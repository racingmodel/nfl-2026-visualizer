"""Prospective, frozen-input V4 forecasts. Never writes official run state."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from nfl2026.phase7.common import json_write
from nfl2026.phase7.player_v4 import reconcile_candidate_td
from nfl2026.utilities.io import sha256_file

POSITIONS = {"QB", "RB", "FB", "WR", "TE"}
KEYS = ["game_id", "player_id", "team"]
FIELDS = {"carries": "expected_carries", "targets": "expected_targets",
          "rushing_yards": "expected_rush_yards", "receiving_yards": "expected_receiving_yards",
          "anytime_offensive_td": "reconciled_td_probability"}


def evaluation_policy():
    """Predeclare populations, zero-entry handling and alerts, not tuned to Week6."""
    return {"version": "phase7-shadow-evaluation-v1", "automatic_promotion": False,
        "primary": "identical original incumbent player/game/team keys",
        "rushing_positions": ["QB", "RB", "FB"], "receiving_positions": ["WR", "TE", "RB", "FB"],
        "nonparticipation": "zero only after validated complete-week ingestion; missing source stays pending",
        "point_metrics": ["MAE", "RMSE", "bias"], "TD_metrics": ["Brier", "log_loss", "reliability_bins"],
        "secondary": "frozen incumbent meaningful-opportunity masks, never actual-volume selection",
        "alerts": "paired season-week bootstrap CI for historical research; paired game-block CI for weekly evaluation; positive MAE delta lower bound triggers review, never automatic promotion",
        "promotion": "all lab gates plus independent prospective multiweek evidence; one week cannot certify",
        "new_outputs": "receptions/snap/red-zone evaluated where targets measured; no incumbent baseline invented"}


def validate_inputs(features, official, games, feature_names, *, season, week, cutoff, now, source_completed_week):
    cutoff, now = pd.Timestamp(cutoff), pd.Timestamp(now)
    if cutoff.tzinfo is None or now.tzinfo is None or cutoff > now:
        raise ValueError("Unknown/future frozen cutoff")
    if games.game_id.duplicated().any() or official.duplicated(KEYS).any():
        raise ValueError("Ambiguous frozen prediction identity")
    if not set(official.position).issubset(POSITIONS) or not set(official.game_id).issubset(games.game_id):
        raise ValueError("Frozen player universe invalid")
    block = features[features.season.eq(season) & features.week.eq(week)].copy()
    if block.duplicated(KEYS).any() or not set(feature_names).issubset(block):
        raise ValueError("Frozen feature contract missing/duplicated")
    block = official[KEYS].merge(block, on=KEYS, how="left", validate="one_to_one", indicator=True)
    if block._merge.ne("both").any():
        raise ValueError("Incumbent universe lacks exact feature rows")
    block = block.drop(columns="_merge")
    known = pd.to_datetime(block.prediction_cutoff_utc, utc=True, errors="coerce")
    if known.isna().any() or known.gt(cutoff).any() or block.feature_version.ne("advanced-pit-v2").any():
        raise ValueError("Frozen feature provenance incompatible")
    if source_completed_week != week - 1:
        raise ValueError("Target-week or unknown outcome feature history")
    for field in ("source_latest_week", "source_actuals_latest_week"):
        if field in block and (pd.to_numeric(block[field], errors="coerce").isna().any() or pd.to_numeric(block[field]).ge(week).any()):
            raise ValueError("Target-week or unknown outcome feature history")
    if any(any(t in f.lower() for t in ("actual_", "target_", "odds", "bookmaker")) for f in feature_names):
        raise ValueError("Outcome/market model feature forbidden")
    kickoff = pd.to_datetime(games.kickoff_utc, utc=True, errors="coerce")
    if kickoff.isna().any() or kickoff.le(cutoff).any():
        raise ValueError("Official frozen run was not pregame")
    return block, bool(kickoff.gt(now).all())


def predict_v4(artifact, frame, games):
    models, features = artifact["models"], artifact["features"]
    d = frame[KEYS + ["season", "week", "player_name", "position"]].copy()
    x = frame[features]
    with threadpool_limits(2):
        for field in ("carries", "targets", "red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets", "snap_share"):
            d[f"pred_{field}"] = np.clip(models[field].predict(x), 0, 1 if field == "snap_share" else None)
        for field, count in (("rushing_yards", "carries"), ("receiving_yards", "targets")):
            d[f"pred_{field}"] = d[f"pred_{count}"] * models[field]["efficiency"].predict(x)
            d[f"direct_{field}"] = np.maximum(0, models[field]["direct"].predict(x))
        d["catch_probability"] = models["catch"].predict_proba(x)[:, 1]
        d["pred_receptions"] = d.pred_targets * d.catch_probability
        d["raw_td_probability"] = 0.
        for name, positions in {"QB": ["QB"], "RB_FB": ["RB", "FB"], "WR_TE": ["WR", "TE"]}.items():
            part = d.position.isin(positions)
            bundle = models[f"td_{name}"]
            if part.any():
                d.loc[part, "raw_td_probability"] = bundle["model"].predict_proba(frame.loc[part, bundle["features"]])[:, 1]
    points = {(r.game_id, r.home_team): r.home_expected_points for r in games.itertuples()}
    points.update({(r.game_id, r.away_team): r.away_expected_points for r in games.itertuples()})
    d["pred_anytime_offensive_td"] = np.nan
    for (game, team), indices in d.groupby(["game_id", "team"]).groups.items():
        budget = float(np.clip(points[(game, team)] / 7 * .72, .35, 4.25))
        d.loc[indices, "pred_anytime_offensive_td"] = reconcile_candidate_td(d.loc[indices, "raw_td_probability"], budget)
    d = d.merge(games[["game_id", "kickoff_utc"]], on="game_id", validate="many_to_one")
    if not np.isfinite(d.filter(regex="^(pred_|direct_|raw_td_|catch_)").to_numpy()).all():
        raise ValueError("Nonfinite challenger predictions")
    return d


def generate_shadow(settings, season, week, run_id, *, kind="extra_trees", allow_retrospective=False):
    """Use the original universe, not today's roster or box-score participation."""
    from nfl2026.production.core import verify_lock
    from nfl2026.live.partial_week import classify_game_eligibility
    from nfl2026.postgame import _frame_digest
    if Path(run_id).name != run_id or run_id in {".",".."} or kind not in {"extra_trees","ridge"}:
        raise ValueError("Explicit official run/candidate identity required")
    started = datetime.now(timezone.utc)
    run = settings.prediction_root / f"{season}/week_{week:02d}" / run_id
    meta = json.loads((run / "metadata.json").read_text())
    if meta["season"] != season or meta["week"] != week or meta["run_id"] != run_id or not meta["deployment_eligible"]:
        raise ValueError("Official reference contract mismatch")
    for name, digest in meta["file_hashes"].items():
        if sha256_file(run / name) != digest:
            raise ValueError("Official forecast hash mismatch")
    lock = settings.root / f"locks/{season}/week_{week:02d}" / meta["lock_id"]
    if verify_lock(lock):
        raise ValueError("Official lock hash mismatch")
    manifest_rows = pd.read_csv(lock / "lock_manifest.csv")
    for field in ("known_at_utc", "retrieved_at_utc"):
        times = pd.to_datetime(manifest_rows[field], utc=True, errors="coerce", format="mixed")
        if times.isna().any() or times.gt(pd.Timestamp(meta["cutoff_utc"])).any():
            raise ValueError("Frozen source contains unknown/future publication or retrieval")
    locked = json.loads((lock / "data_lock.json").read_text())
    source = lock / "inputs/player_pregame_universe_features.csv"
    archive = locked.get("exact_feature_archive", {})
    if archive.get("status") != "READY" or sha256_file(source) != archive["source_sha256"][source.name]:
        raise ValueError("Exact frozen feature bytes unavailable")
    artifact_path = settings.artifact_root / f"research/phase7/models/player-v4-{kind}-train-through-2024.joblib"
    record = json.loads(artifact_path.with_suffix(".json").read_text())
    if sha256_file(artifact_path) != record["sha256"] or pd.Timestamp(record["provenance"]["max_training_kickoff_utc"]) >= pd.Timestamp(started):
        raise ValueError("Candidate artifact hash/training cutoff unsafe")
    artifact = joblib.load(artifact_path)
    if artifact["features"] != record["provenance"]["feature_schema"]:
        raise ValueError("Candidate schema does not match fitted artifact")
    games = pd.read_csv(run / "games.csv")
    if pd.Timestamp(record["provenance"]["max_training_kickoff_utc"]) >= pd.to_datetime(games.kickoff_utc,utc=True).min():
        raise ValueError("Candidate was trained after a target kickoff, even for retrospective replay")
    official = pd.read_csv(run / "players.csv")
    features = pd.read_csv(source, low_memory=False)
    rolling_path = settings.artifact_root / f"phase2/rollforward_manifests/{season}/week_{week:02d}.json"
    rolling = json.loads(rolling_path.read_text())
    target = features[features.season.eq(season) & features.week.eq(week)].reset_index(drop=True)
    if (rolling["target_week"] != week or rolling["season"] != season or
        rolling["feature_rows_sha256"] != _frame_digest(target) or
        pd.Timestamp(rolling["generated_at_utc"]) > pd.Timestamp(meta["cutoff_utc"])):
        raise ValueError("Rolling source provenance is not bound to frozen target features")
    actual_manifest = settings.root / rolling["source_actual_manifest_path"]
    if sha256_file(actual_manifest) != rolling["source_actual_manifest_sha256"]:
        raise ValueError("Prior-week actual source provenance invalid")
    block, prospective = validate_inputs(features, official, games, artifact["features"],
        season=season, week=week, cutoff=meta["cutoff_utc"], now=started,
        source_completed_week=rolling["source_completed_week"])
    # An up-to-date schedule may disqualify an early/changed kickoff. It cannot
    # certify inputs retrospectively or supply any numerical feature.
    schedule = pd.read_csv(settings.root / "data/phase2_external/games.csv", low_memory=False)
    states = classify_game_eligibility(schedule, season, week, started)
    if set(states.game_id) != set(games.game_id):
        raise ValueError("Current schedule and frozen game IDs disagree")
    prospective = prospective and states.eligibility_state.eq("PRE_KICKOFF").all()
    if not prospective and not allow_retrospective:
        raise ValueError("Pregame shadow no longer possible; explicit retrospective replay required")
    predictions = predict_v4(artifact, block.reset_index(drop=True), games)
    comparison = predictions.merge(official[KEYS + list(FIELDS.values())], on=KEYS, validate="one_to_one")
    completed_at = datetime.now(timezone.utc)
    earliest = min(pd.to_datetime(games.kickoff_utc, utc=True).min(), pd.to_datetime(states.kickoff_utc, utc=True).min())
    prospective = prospective and pd.Timestamp(completed_at) < earliest
    if not prospective and not allow_retrospective:
        raise ValueError("Kickoff occurred during inference; no prospective archive created")
    destination = settings.artifact_root / f"research/phase7/shadows/{season}/week_{week:02d}"
    folder = destination / f"shadow-{completed_at.strftime('%Y%m%dT%H%M%S%fZ')}-{kind}"
    folder.mkdir(parents=True, exist_ok=False)
    # Original byte snapshots and model artifact are retained for replay. The
    # consumed target-block CSV is separate; neither rewrites any frozen input.
    shutil.copytree(lock, folder / "official_lock")
    shutil.copyfile(artifact_path, folder / artifact_path.name)
    shutil.copyfile(artifact_path.with_suffix(".json"), folder / artifact_path.with_suffix(".json").name)
    shutil.copyfile(run / "players.csv", folder / "incumbent_players.csv")
    shutil.copyfile(run / "games.csv", folder / "incumbent_games.csv")
    shutil.copyfile(rolling_path, folder / "rollforward_provenance.json")
    shutil.copyfile(actual_manifest, folder / "prior_week_actuals_manifest.json")
    block.to_csv(folder / "consumed_features.csv", index=False)
    predictions.to_csv(folder / "predictions.csv", index=False)
    comparison.to_csv(folder / "comparison.csv", index=False)
    json_write(folder / "evaluation_policy.json", evaluation_policy())
    for name in ("shadow.py", "player_v4.py"):
        shutil.copyfile(Path(__file__).with_name(name), folder / name)
    finalized = datetime.now(timezone.utc)
    prospective = prospective and pd.Timestamp(finalized) < earliest
    if not prospective and not allow_retrospective:
        raise ValueError("Archive crossed kickoff; incomplete folder is NOT a certified shadow")
    manifest = {"status": "PROSPECTIVE_SHADOW" if prospective else "RETROSPECTIVE_REPLAY",
        "created_at_utc": finalized.isoformat(), "inference_started_at_utc": started.isoformat(),
        "prediction_cutoff_utc": completed_at.isoformat(), "original_input_cutoff_utc": meta["cutoff_utc"],
        "season": season, "week": week, "official_run_id": run_id, "official_lock_id": lock.name,
        "players": len(predictions), "games": len(games), "same_incumbent_universe": True,
        "candidate_id": artifact_path.stem, "training_provenance": record["provenance"],
        "eligibility_scope": "exact official frozen universe, not complete independent eligibility certification",
        "source_states": pd.read_csv(lock / "lock_manifest.csv").fillna("").to_dict("records"),
        "official_pointer_updated": False, "production_promoted": False,
        "files": {str(p.relative_to(folder)): sha256_file(p) for p in folder.rglob("*") if p.is_file()}}
    json_write(folder / "manifest.json", manifest)
    return folder


def verify_shadow(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    for relative, digest in manifest["files"].items():
        if sha256_file(folder / relative) != digest:
            raise ValueError(f"Shadow integrity failed: {relative}")
    if manifest["status"] == "PROSPECTIVE_SHADOW":
        rows = pd.read_csv(folder / "predictions.csv")
        if pd.Timestamp(manifest["created_at_utc"]) >= pd.to_datetime(rows.kickoff_utc, utc=True).min():
            raise ValueError("Shadow archived after kickoff")
    return manifest
