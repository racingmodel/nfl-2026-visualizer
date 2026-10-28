"""Result matching for archived shadows, separate from immutable forecasts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from nfl2026.five_week_review import finite, probability_metrics, block_bootstrap_difference
from nfl2026.phase7.common import json_write
from nfl2026.phase7.shadow import FIELDS, KEYS, verify_shadow
from nfl2026.utilities.io import sha256_file


def paired_metrics(frame):
    rows, calibration = [], []
    groups = [("ALL", frame)] + [(p, frame[frame.position.eq(p)]) for p in sorted(frame.position.unique())]
    groups += [("FROZEN_MEANINGFUL_OPPORTUNITY", frame)]
    if 'role_status' in frame:
        groups += [('FROZEN_EXPECTED_STARTER',frame[frame.role_status.eq('EXPECTED_STARTER')]),
            ('FROZEN_BACKUP_OR_UNCERTAIN',frame[~frame.role_status.eq('EXPECTED_STARTER')])]
    groups += [('FROZEN_LOW_OPPORTUNITY',frame[(frame.expected_carries+frame.expected_targets)<5]),
        ('FROZEN_HIGH_OPPORTUNITY',frame[(frame.expected_carries+frame.expected_targets)>=5])]
    for segment, part in groups:
        for target, incumbent in FIELDS.items():
            cohort = part
            if target in {"carries", "rushing_yards"}:
                cohort = part[part.position.isin(["QB", "RB", "FB"])]
            elif target in {"targets", "receiving_yards"}:
                cohort = part[part.position.isin(["WR", "TE", "RB", "FB"])]
            if segment == "FROZEN_MEANINGFUL_OPPORTUNITY":
                if target in {"carries", "rushing_yards"}:
                    cohort=cohort[cohort.expected_carries.ge(1)|cohort.expected_rush_yards.ge(5)]
                elif target in {"targets", "receiving_yards"}:
                    cohort=cohort[cohort.expected_targets.ge(1)|cohort.expected_receiving_yards.ge(8)]
                else:
                    continue  # Primary TD cohort is all original offensive rows.
            if cohort.empty:
                continue
            actual, champion, challenger = (cohort[f"actual_{target}"], cohort[incumbent], cohort[f"pred_{target}"])
            if target == "anytime_offensive_td":
                for name, probabilities in (("champion", champion), ("challenger", challenger)):
                    rows.append({"segment": segment, "target": target, "method": name, "rows": len(cohort),
                        **probability_metrics(actual, probabilities)})
                    for lower in np.arange(0, 1, .1):
                        mask = probabilities.ge(lower) & (probabilities.lt(lower+.1) if lower < .9 else probabilities.le(1))
                        calibration.append({"segment": segment, "method": name, "lower": float(lower), "rows": int(mask.sum()),
                            "predicted": float(probabilities[mask].mean()) if mask.any() else None,
                            "observed": float(actual[mask].mean()) if mask.any() else None})
            else:
                difference = (challenger-actual).abs() - (champion-actual).abs()
                # Weekly game blocks, not individual players treated as independent.
                blocks = cohort.copy()
                blocks["season"], blocks["week"] = 0, pd.factorize(blocks.game_id)[0]
                _, low, high = block_bootstrap_difference(blocks, difference.to_numpy(), 400)
                for name, predicted in (("champion", champion), ("challenger", challenger)):
                    error = predicted-actual
                    rows.append({"segment": segment, "target": target, "method": name, "rows": len(cohort),
                        "mae": float(error.abs().mean()), "rmse": float(np.sqrt((error**2).mean())), "bias": float(error.mean()),
                        "paired_mae_delta": float(difference.mean()), "delta_ci_low": low, "delta_ci_high": high,
                        "regression_review_alert": bool(low > 0)})
    return pd.DataFrame(rows), calibration


def evaluate_shadow(settings, folder):
    from nfl2026.postgame import inspect_completed_week_actuals, _completion_mask
    manifest = verify_shadow(folder)
    season, week = manifest["season"], manifest["week"]
    schedule = pd.read_csv(settings.root / "data/phase2_external/games.csv", low_memory=False)
    games = schedule[schedule.season.eq(season) & schedule.week.eq(week) & schedule.game_type.eq("REG")]
    forecasts = pd.read_csv(folder / "comparison.csv")
    # Role subgroups must use frozen incumbent annotations, never postgame depth.
    if (folder/'incumbent_players.csv').exists():
        incumbent=pd.read_csv(folder/'incumbent_players.csv')
        if 'role_status' not in forecasts and 'role_status' in incumbent:
            forecasts=forecasts.merge(incumbent[KEYS+['role_status']],on=KEYS,validate='one_to_one')
    if set(games.game_id) != set(forecasts.game_id):
        raise ValueError("Shadow/current schedule game identity mismatch")
    if not _completion_mask(games).all():
        return {"status": "PENDING_FINALITY", "final_games": int(_completion_mask(games).sum()), "games": len(games),
                "shadow_status": manifest["status"], "forecasts_mutated": False}
    ingestion = inspect_completed_week_actuals(settings, season, week, games)
    if ingestion["status"] != "ALREADY_CURRENT":
        return {"status": "PENDING_VALIDATED_ACTUALS", "actuals_state": ingestion["status"], "forecasts_mutated": False}
    actual_path = settings.artifact_root / "canonical/player_weekly.csv"
    actual = pd.read_csv(actual_path, low_memory=False)
    actual = actual[actual.season.eq(season) & actual.week.eq(week) & actual.player_id.notna()]
    identity = ["player_id", "team"]
    if actual.duplicated(identity).any():
        raise ValueError("Ambiguous completed player observations")
    optional = ["receptions"] if "receptions" in actual else []
    actual = actual[identity + list(FIELDS) + optional].rename(columns={f: f"actual_{f}" for f in list(FIELDS)+optional})
    matched = forecasts.merge(actual, on=identity, how="left", validate="one_to_one", indicator=True)
    matched["actual_nonparticipant"] = matched._merge.eq("left_only")
    # Only absent entries may become zero. Unknown statistics on a present row
    # fail closed rather than being silently labelled nonparticipation.
    for target in FIELDS:
        column = f"actual_{target}"
        if matched.loc[~matched.actual_nonparticipant, column].isna().any():
            raise ValueError("Present actual player has missing required statistics")
        matched.loc[matched.actual_nonparticipant, column] = 0
    metrics, reliability = paired_metrics(matched)
    extra_metrics = []
    if "actual_receptions" in matched:
        matched.loc[matched.actual_nonparticipant, "actual_receptions"] = 0
        measured = matched[matched.position.ne("QB") & matched.actual_receptions.notna()]
        for position, cohort in [("ALL", measured)] + list(measured.groupby("position")):
            error = cohort.pred_receptions - cohort.actual_receptions
            if len(cohort):
                extra_metrics.append({"target":"receptions", "method":"challenger", "segment":position,"rows":len(cohort),
                    "mae":float(error.abs().mean()),"rmse":float(np.sqrt((error**2).mean())),"bias":float(error.mean()),
                    "incumbent_comparison":"NOT_AVAILABLE_NO_PRODUCTION_RECEPTIONS_MODEL"})
    metrics = pd.concat([metrics,pd.DataFrame(extra_metrics)],ignore_index=True)
    digest = sha256_file(actual_path)
    destination = settings.artifact_root / f"research/phase7/evaluations/{folder.name}-{digest[:12]}"
    if destination.exists():
        saved = json.loads((destination / "manifest.json").read_text())
        for name, expected in saved["files"].items():
            if sha256_file(destination / name) != expected:
                raise ValueError("Archived evaluation modified")
        return {**saved, "status": "ALREADY_EVALUATED"}
    destination.mkdir(parents=True, exist_ok=False)
    matched.drop(columns="_merge").to_csv(destination / "matched.csv", index=False)
    metrics.to_csv(destination / "metrics.csv", index=False)
    json_write(destination / "calibration.json", finite(reliability))
    result = {"status": "EVALUATED", "shadow_status": manifest["status"], "season": season, "week": week,
        "evaluated_at_utc":datetime.now(timezone.utc).isoformat(),
        "rows": len(matched), "nonparticipants": int(matched.actual_nonparticipant.sum()),
        "shadow_manifest_sha256": sha256_file(folder / "manifest.json"), "actuals_sha256": digest,
        "automatic_promotion": False, "optional_snap_redzone":"not scored without separately validated completed source coverage",
        "alerts": finite(metrics[metrics.get("regression_review_alert", pd.Series(False,index=metrics.index)).eq(True)].to_dict("records")),
        "files": {p.name: sha256_file(p) for p in destination.iterdir() if p.is_file()}}
    json_write(destination / "manifest.json", result)
    history = [json.loads(p.read_text()) for p in destination.parent.glob("*/manifest.json")]
    json_write(settings.report_root / "phase7_shadow_history.json", {"evaluations": history, "automatic_promotion": False})
    return result
