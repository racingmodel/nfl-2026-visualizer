"""Read-only official evaluation plus a separate immutable challenger registry."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.five_week_review import finite, team_metrics, probability_metrics, yard_metrics
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file

GATES = ("pit_provenance", "time_ordered", "primary_improvement", "cross_season_stability",
         "calibration_noninferiority", "bias_acceptable", "uncertainty_quality", "reproducible_inference",
         "runtime_acceptable", "reliable_current_inputs", "production_tests")


def promotion_decision(evidence: dict) -> dict:
    # Truthy strings such as "PARTIAL" are NOT evidence of a passed gate.
    blocked = [gate for gate in GATES if evidence.get(gate) is not True]
    return {"decision": "ELIGIBLE_FOR_HUMAN_APPROVAL" if not blocked else "DO_NOT_PROMOTE",
            "blocked_gates": blocked, "automatic_activation": False}


def register_challenger(destination: Path, model_id: str, artifact: Path, metadata: dict) -> Path:
    if not model_id or Path(model_id).name != model_id or model_id in {".", ".."}:
        raise ValueError("Invalid challenger ID")
    required = {"training_cutoff", "feature_schema", "validation_periods", "metrics", "provenance_certified"}
    if not required.issubset(metadata) or not artifact.is_file():
        raise ValueError("Challenger metadata/artifact incomplete")
    record = {"model_id": model_id, "artifact_sha256": sha256_file(artifact), "metadata": metadata,
              "status": "EXPERIMENTAL", "production_selected": False}
    path = destination / f"{model_id}.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != record:
            raise FileExistsError("Challenger version is immutable")
    else:
        json_write(path, record)
    return path


def archive_shadow(destination: Path, shadow_id: str, predictions: pd.DataFrame, *,
                   cutoff, input_files: list[Path], model_id: str, training_cutoff,
                   model_artifact: Path | None = None, input_evidence: dict | None = None) -> Path:
    """Capture input bytes and provenance before accepting shadow predictions.

    Caller generates candidates offline. This never runs or modifies official
    predictions. Historical retrocasts cannot masquerade as contemporaneous shadows.
    """
    if Path(shadow_id).name != shadow_id or shadow_id in {".", ".."}:
        raise ValueError("Invalid shadow ID")
    cutoff = pd.Timestamp(cutoff)
    trained = pd.Timestamp(training_cutoff)
    if cutoff.tzinfo is None or trained.tzinfo is None or trained >= cutoff:
        raise ValueError("Shadow training cutoff unsafe")
    if not {"game_id", "kickoff_utc"}.issubset(predictions) or predictions.empty:
        raise ValueError("Shadow prediction identity incomplete")
    kickoff = pd.to_datetime(predictions.kickoff_utc, utc=True, errors="coerce")
    if kickoff.isna().any() or kickoff.le(cutoff).any():
        raise ValueError("Shadow created at/after kickoff")
    if not input_files:
        raise ValueError("Exact shadow inputs required")
    names = [p.name for p in input_files]
    if len(set(names)) != len(names) or set(names) & {"manifest.json", "shadow_predictions.csv"}:
        raise ValueError("Shadow input names collide")
    hashes = {}
    for path in input_files:
        data = pd.read_csv(path)
        if "known_at_utc" not in data:
            raise ValueError("Shadow input known-at provenance missing")
        known = pd.to_datetime(data.known_at_utc, utc=True, errors="coerce")
        if known.isna().any() or known.gt(cutoff).any():
            raise ValueError("Shadow future/unknown source time")
        if any(any(token in c.lower() for token in ("bookmaker", "moneyline", "odds", "target_", "actual_")) for c in data):
            raise ValueError("Shadow input contains outcome/market fields")
        hashes[path.name] = sha256_file(path)
    from nfl2026.phase7.provenance import certified_pregame
    for path in input_files:
        evidence = (input_evidence or {}).get(path.name, {})
        if evidence.get("source_sha256") != hashes[path.name] or not certified_pregame(evidence, cutoff):
            raise ValueError("Shadow input independently captured source evidence missing/incompatible")
    if model_artifact is None or not model_artifact.is_file() or not model_artifact.with_suffix(".json").is_file():
        raise ValueError("Exact challenger artifact/provenance required")
    model_meta = json.loads(model_artifact.with_suffix(".json").read_text(encoding="utf-8"))
    model_hash = sha256_file(model_artifact)
    provenance = model_meta.get("provenance", {})
    if model_meta.get("sha256") != model_hash or pd.Timestamp(provenance.get("max_training_kickoff_utc")) != trained:
        raise ValueError("Shadow challenger training/hash contract mismatch")
    folder = destination / shadow_id
    folder.mkdir(parents=True, exist_ok=False)
    for path in input_files:
        content = path.read_bytes()
        import hashlib
        if hashlib.sha256(content).hexdigest() != hashes[path.name]:
            raise ValueError("Shadow source changed during archival")
        (folder / path.name).write_bytes(content)
    predictions.to_csv(folder / "shadow_predictions.csv", index=False)
    json_write(folder / "manifest.json", {"status": "SHADOW_NOT_OFFICIAL", "model_id": model_id,
        "cutoff": cutoff.isoformat(), "training_cutoff": trained.isoformat(), "input_hashes": hashes,
        "input_evidence":input_evidence, "model_artifact_sha256":model_hash,
        "prediction_sha256": sha256_file(folder / "shadow_predictions.csv"), "official_pointer_updated": False})
    return folder


def evaluate_official(settings) -> tuple[dict, pd.DataFrame]:
    from nfl2026.production.core import verify_lock
    schedule = pd.read_csv(settings.root / "data/phase2_external/games.csv", low_memory=False)
    actual = pd.read_csv(settings.artifact_root / "canonical/player_weekly.csv", low_memory=False)
    actual = actual[actual.season.eq(2026) & actual.player_id.notna()]
    if actual.duplicated(["season", "week", "player_id", "team"]).any():
        raise ValueError("Actual player identity duplicated")
    game_parts, player_parts, cohorts = [], [], []
    weeks = sorted(int(p.name.removeprefix("week_")) for p in
        (settings.artifact_root / "official_freezes/2026").glob("week_*") if p.is_dir())
    for week in weeks:
        freezes = sorted((settings.artifact_root / f"official_freezes/2026/week_{week:02d}").glob("freeze-*"))
        if not freezes:
            continue
        freeze = freezes[-1]
        selection = pd.read_csv(freeze / "official_predictions.csv")
        expected = schedule[schedule.season.eq(2026) & schedule.week.eq(week) & schedule.game_type.eq("REG")]
        if selection.game_id.duplicated().any() or set(selection.game_id) != set(expected.game_id):
            raise ValueError("Official freeze/schedule mismatch")
        from nfl2026.postgame import _completion_mask, inspect_completed_week_actuals
        completed = expected[_completion_mask(expected)]
        ingestion = inspect_completed_week_actuals(settings, 2026, week, expected) if len(completed)==len(expected) else {
            "status":"WAITING_WEEK_FINALITY"}
        for run_id, group in selection.groupby("official_prediction_run_id"):
            run = settings.prediction_root / f"2026/week_{week:02d}" / run_id
            meta = json.loads((run / "metadata.json").read_text())
            for name, digest in meta["file_hashes"].items():
                if sha256_file(run / name) != digest:
                    raise ValueError("Official artifact integrity failure")
            lock = settings.root / f"locks/2026/week_{week:02d}" / meta["lock_id"]
            if verify_lock(lock):
                raise ValueError("Official input lock integrity failure")
            if (pd.to_datetime(group.official_prediction_timestamp, utc=True) >= pd.to_datetime(group.kickoff_utc, utc=True)).any():
                raise ValueError("Official forecast was not pregame")
            ids = set(group.game_id) & set(completed.game_id) if ingestion["status"] == "ALREADY_CURRENT" else set()
            cohorts.append({"week": week, "freeze_id": freeze.name, "run_id": run_id,
                "evaluated_games": len(ids), "frozen_games": len(group), "pending_games": len(group)-len(ids),
                "actuals_source_state":ingestion["status"]})
            if not ids:
                continue
            # Authoritative completed-week state validates actual ingestion, not
            # whether a convenient player CSV happens to expose game_id.
            g = pd.read_csv(run / "games.csv")
            g = g[g.game_id.isin(ids)].merge(completed[["game_id", "home_score", "away_score"]], on="game_id", validate="one_to_one")
            g["pred_home_score"], g["pred_away_score"] = g.home_expected_points, g.away_expected_points
            g["actual_home_score"], g["actual_away_score"] = g.home_score, g.away_score
            g["week"], g["source_run_id"] = week, run_id
            game_parts.append(g)
            p = pd.read_csv(run / "players.csv")
            p = p[p.game_id.isin(ids) & p.position.isin(["QB", "RB", "FB", "WR", "TE"])].copy()
            p["season"], p["week"] = 2026, week
            columns = ["season", "week", "team", "player_id", "carries", "targets", "rushing_yards", "receiving_yards", "anytime_offensive_td"]
            p = p.merge(actual[columns], on=["season", "week", "team", "player_id"], how="left", validate="one_to_one")
            for field in columns[4:]:
                p[f"actual_{field}"] = p[field].fillna(0)
            player_parts.append(p)
    games = pd.concat(game_parts, ignore_index=True)
    players = pd.concat(player_parts, ignore_index=True)
    rows = []
    for label, game_group, player_group in [("ALL", games, players)] + [(f"WEEK_{w}", games[games.week.eq(w)], players[players.week.eq(w)]) for w in sorted(games.week.unique())]:
        row = {"segment": label, **team_metrics(game_group), "offensive_player_rows": len(player_group),
            **{"td_"+k:v for k,v in probability_metrics(player_group.actual_anytime_offensive_td, player_group.reconciled_td_probability).items()}}
        for kind, field, expected, mask in [
            ("rush", "rushing_yards", "expected_rush_yards", player_group.expected_carries.ge(1) | player_group.expected_rush_yards.ge(5)),
            ("receiving", "receiving_yards", "expected_receiving_yards", player_group.expected_targets.ge(1) | player_group.expected_receiving_yards.ge(8))]:
            part = player_group[mask]
            row.update({f"{kind}_{k}":v for k,v in yard_metrics(part[f"actual_{field}"], part[expected]).items()})
            row[f"{kind}_rows"] = len(part)
        rows.append(row)
    pd.DataFrame(rows).to_csv(settings.report_root / "phase7_official_performance.csv", index=False)
    pd.DataFrame(cohorts).to_csv(settings.report_root / "phase7_official_cohorts.csv", index=False)
    return finite({"metrics": rows, "cohorts": cohorts, "games": len(games), "offensive_player_rows": len(players),
        "qualifications": ["Original pregame freeze per game; no retrospective regeneration",
            "TD comparable population excludes defensive Week2 rows", "Missing box-score entry for a predicted player scored as zero after validated complete ingestion",
            "Completed weeks are monitoring evidence, not tuning data", "Missing unpredicted actual players reported in prior five-week coverage audit; metrics here score original forecasts"]}), games


def build_lab(settings):
    data, _ = evaluate_official(settings)
    json_write(settings.report_root / "phase7_performance.json", data)
    directory = settings.artifact_root / "research/phase7/models"
    registered = []
    for artifact in sorted(directory.glob("player-v4-*.joblib")):
        record = json.loads(artifact.with_suffix(".json").read_text())
        provenance = record["provenance"]
        if sha256_file(artifact) != record["sha256"]:
            raise ValueError("Challenger hash mismatch")
        meta = {"training_cutoff": provenance["max_training_kickoff_utc"],
            "feature_schema": provenance["feature_schema"], "validation_periods": [max(provenance["training_seasons"])+1],
            "metrics": "reports/phase7_player_v4_metrics.csv", "provenance_certified": False,
            "promotion_decision": promotion_decision({"time_ordered": True, "pit_provenance": False})}
        register_challenger(settings.artifact_root / "research/phase7/challengers", artifact.stem, artifact, meta)
        registered.append({"model_id": artifact.stem, **meta})
    for name, metric_path, schema in (
        ("conditional-residual-research-v1", "phase7_threshold_distribution_backtest.csv", ["predicted_mean", "position"]),
        ("qb-passing-roster-research-v1", "phase7_qb_passing_metrics.csv", None)):
        artifact = directory / f"{name}.joblib"
        if not artifact.exists():
            continue
        record = json.loads(artifact.with_suffix(".json").read_text())
        provenance = record["provenance"]
        if sha256_file(artifact) != record["sha256"]:
            raise ValueError("Additional research artifact hash mismatch")
        if schema is None:
            import joblib
            schema = joblib.load(artifact)["features"]
        meta = {"training_cutoff":provenance.get("max_training_kickoff_utc", provenance.get("max_calibration_kickoff")),
            "feature_schema":schema,"validation_periods":[2025],"metrics":f"reports/{metric_path}",
            "provenance_certified":False,"promotion_decision":promotion_decision({"pit_provenance":False})}
        register_challenger(settings.artifact_root / "research/phase7/challengers", name, artifact, meta)
        registered.append({"model_id":name,**meta})
    current = json.loads((settings.root / "frontend/dist/data/current_week.json").read_text())["metadata"]
    run = settings.prediction_root / f"2026/week_{int(current['week']):02d}" / current["run_id"]
    run_meta = json.loads((run / "metadata.json").read_text())
    source_manifest = pd.read_csv(settings.root / f"locks/2026/week_{int(current['week']):02d}" / run_meta["lock_id"] / "lock_manifest.csv")
    transaction = source_manifest[source_manifest.source.eq("transactions")]
    transaction_state = str(transaction.iloc[-1].data_status) if len(transaction) else "SOURCE_MISSING"
    from nfl2026.phase7.shadow import verify_shadow
    shadows = []
    for path in sorted((settings.artifact_root / "research/phase7/shadows").glob("*/week_*/*/manifest.json")):
        capture = verify_shadow(path.parent)
        shadows.append({"week":capture["week"], "season":capture["season"], "status":capture["status"],
            "players":capture["players"], "candidate_id":capture["candidate_id"], "cutoff":capture["prediction_cutoff_utc"]})
    json_write(settings.report_root / "phase7_lab.json", {"champions": {"team":"phase2-v2", "TD":"phase2-v2", "player":"phase2-v3-rush-et"},
        "challengers": registered, "automatic_promotion": False, "shadow_outputs": shadows,
        "health": {"frozen_integrity": "VERIFIED", "current_transactions": transaction_state,
            "transaction_warnings": transaction.quality_warnings.fillna("").tolist() if len(transaction) else ["source missing"],
            "historical_eligibility": "BLOCKED", "calibration_certification": "PARTIAL", "research_evidence_not_live_promoted": True}})
