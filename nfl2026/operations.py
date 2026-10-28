from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from nfl2026.config import Settings
from nfl2026.production.evaluation import select_evaluation_runs


STAGE_ALIASES = {
    "early": "EARLY_WEEK",
    "early_week": "EARLY_WEEK",
    "mid": "MID_WEEK",
    "mid_week": "MID_WEEK",
    "final": "FINAL",
    "game_day": "GAME_DAY",
    "gameday": "GAME_DAY",
}


def normalise_stage(value: str, allowed: tuple[str, ...] | None = None) -> str:
    stage = STAGE_ALIASES.get(value.strip().lower(), value.strip().upper())
    permitted = allowed or ("EARLY_WEEK", "MID_WEEK", "FINAL", "GAME_DAY")
    if stage not in permitted:
        raise ValueError(f"Unsupported stage {value!r}; choose {', '.join(permitted)}")
    return stage


def eligible_stage_runs(settings: Settings, season: int, week: int, stage: str) -> list[Path]:
    stage = normalise_stage(stage, settings.operation_stages)
    root = settings.prediction_root / str(season) / f"week_{week:02d}"
    rows: list[tuple[pd.Timestamp, str, Path]] = []
    for metadata_path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                meta.get("weekly_stage") == stage
                and meta.get("run_status") == "OFFICIAL_PRODUCTION"
                and meta.get("validation_result") == "PASS"
                and meta.get("deployment_eligible") is True
                and meta.get("prediction_scope", "FULL_WEEK") != "UNPLAYED_ONLY"
            ):
                rows.append((pd.Timestamp(meta["created_at_utc"]), str(meta["run_id"]), metadata_path.parent))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return [item[2] for item in sorted(rows)]


def latest_stage_run(settings: Settings, season: int, week: int, stage: str) -> Path | None:
    choices = eligible_stage_runs(settings, season, week, stage)
    return choices[-1] if choices else None


def official_prediction_freeze(settings: Settings, season: int, week: int, selected_at: datetime | None = None) -> pd.DataFrame:
    selected = select_evaluation_runs(settings, season, week, selected_at)
    if selected.empty:
        return pd.DataFrame(columns=[
            "season", "week", "game_id", "kickoff_utc", "official_prediction_run_id",
            "official_prediction_timestamp", "selection_policy", "selected_at_utc",
        ])
    return selected.rename(columns={
        "selected_run_id": "official_prediction_run_id",
        "run_cutoff_utc": "official_prediction_timestamp",
    })


def official_value_freeze(settings: Settings, season: int, week: int, selected_at: datetime | None = None) -> pd.DataFrame:
    selected_at = selected_at or datetime.now(timezone.utc)
    prediction = official_prediction_freeze(settings, season, week, selected_at)
    if prediction.empty:
        return pd.DataFrame(columns=[
            "season", "week", "game_id", "kickoff_utc", "official_value_run_id",
            "market_snapshot_id", "market_timestamp_utc", "selection_policy", "selected_at_utc",
        ])
    kickoffs = prediction.set_index("game_id").kickoff_utc.to_dict()
    root = settings.root / "value_runs" / str(season) / f"week_{week:02d}"
    candidates: list[dict[str, object]] = []
    for metadata_path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            if meta.get("status") != "OFFICIAL_VALUE_SNAPSHOT" or meta.get("mode") != "official":
                continue
            qa = pd.read_csv(metadata_path.parent / "qa_report.csv")
            if qa.empty or not qa.passed.astype(bool).all():
                continue
            values = pd.read_csv(metadata_path.parent / "value_bets.csv")
            for game_id, group in values[values.status.eq("ACTIVE")].groupby("game_id"):
                if str(game_id) not in kickoffs:
                    continue
                market_time = pd.to_datetime(group.market_timestamp_utc, utc=True).max()
                kickoff = pd.Timestamp(kickoffs[str(game_id)])
                if market_time < kickoff and market_time <= pd.Timestamp(selected_at):
                    candidates.append({
                        "season": season,
                        "week": week,
                        "game_id": str(game_id),
                        "kickoff_utc": kickoff.isoformat(),
                        "official_value_run_id": meta["value_run_id"],
                        "market_snapshot_id": meta["market_snapshot_id"],
                        "market_timestamp_utc": market_time.isoformat(),
                    })
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    if not candidates:
        return pd.DataFrame(columns=[
            "season", "week", "game_id", "kickoff_utc", "official_value_run_id",
            "market_snapshot_id", "market_timestamp_utc", "selection_policy", "selected_at_utc",
        ])
    frame = pd.DataFrame(candidates)
    frame["market_timestamp_utc"] = pd.to_datetime(frame.market_timestamp_utc, utc=True)
    result = frame.sort_values(["game_id", "market_timestamp_utc", "official_value_run_id"]).groupby("game_id", as_index=False).tail(1).copy()
    result["market_timestamp_utc"] = result.market_timestamp_utc.map(lambda value: value.isoformat())
    result["selection_policy"] = "latest QA-passed official market observation before kickoff; never result-selected"
    result["selected_at_utc"] = selected_at.isoformat()
    return result.sort_values("game_id").reset_index(drop=True)


def write_freeze_manifest(settings: Settings, season: int, week: int, selected_at: datetime | None = None) -> Path:
    selected_at = selected_at or datetime.now(timezone.utc)
    predictions = official_prediction_freeze(settings, season, week, selected_at)
    values = official_value_freeze(settings, season, week, selected_at)
    identity = f"{season}|{week}|{selected_at.isoformat()}|{','.join(predictions.get('official_prediction_run_id', pd.Series(dtype=str)).astype(str))}"
    freeze_id = f"freeze-{selected_at.strftime('%Y%m%dT%H%M%SZ')}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"
    destination = settings.artifact_root / "official_freezes" / str(season) / f"week_{week:02d}" / freeze_id
    if destination.exists():
        raise FileExistsError(f"Official freeze is immutable: {destination}")
    destination.mkdir(parents=True)
    predictions.to_csv(destination / "official_predictions.csv", index=False, lineterminator="\n")
    values.to_csv(destination / "official_values.csv", index=False, lineterminator="\n")
    metadata = {
        "freeze_id": freeze_id,
        "season": season,
        "week": week,
        "created_at_utc": selected_at.isoformat(),
        "prediction_games": len(predictions),
        "value_games": len(values),
        "prediction_policy": "last valid independent production prediction before each game kickoff",
        "value_policy": "latest QA-passed fresh official market observation before each game kickoff",
    }
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def build_partial_game_lineage(settings: Settings, season: int, week: int, cutoff: datetime,
                               run_id: str, lock_id: str, refresh_reference: str,
                               eligibility: pd.DataFrame, generated_games: pd.DataFrame,
                               generated_at: str | None = None) -> pd.DataFrame:
    """Describe fresh eligible-game predictions and immutable reused predictions."""
    required = {"game_id", "eligibility_state"}
    if not required.issubset(eligibility.columns) or eligibility.game_id.astype(str).duplicated().any():
        raise ValueError("Partial lineage requires unique game eligibility rows")
    selected = select_evaluation_runs(settings, season, week, cutoff)
    old_by_game = {str(row.game_id): row for row in selected.itertuples(index=False)}
    fresh = set(generated_games.game_id.astype(str))
    if generated_games.game_id.astype(str).duplicated().any() or fresh-set(eligibility.game_id.astype(str)):
        raise ValueError("Partial generated fixture IDs are duplicated or outside the eligibility set")
    output = []
    for state in eligibility.itertuples(index=False):
        game_id = str(state.game_id)
        if game_id in fresh:
            row = generated_games[generated_games.game_id.astype(str).eq(game_id)]
            if len(row) != 1 or state.eligibility_state != "PRE_KICKOFF":
                raise ValueError(f"Fresh prediction is not exactly one PRE_KICKOFF game: {game_id}")
            output.append({"game_id": game_id, "game_state_at_run": state.eligibility_state,
                "prediction_source_run_id": run_id, "prediction_generated_at": generated_at or cutoff.isoformat(),
                "prediction_reused_immutable": False, "feature_lock_reference": lock_id,
                "pregame_refresh_reference": refresh_reference,
                "game_prediction_sha256": hashlib.sha256(row.iloc[0].to_json(date_format="iso").encode("utf-8")).hexdigest()})
            continue
        reason=getattr(state,"refresh_gate_reason",None)
        blocked_with_reason = (getattr(state, "refresh_gate_status", "") == "BLOCKED"
                               and pd.notna(reason) and bool(str(reason).strip()))
        if state.eligibility_state not in {"FINAL", "LIVE"} and not blocked_with_reason:
            raise ValueError(f"Partial run cannot omit non-immutable game {game_id}")
        prior = old_by_game.get(game_id)
        if prior is None:
            raise FileNotFoundError(f"Missing original immutable prediction for {game_id}")
        bundle = settings.prediction_root/str(season)/f"week_{week:02d}"/str(prior.selected_run_id)
        source_meta = json.loads((bundle/"metadata.json").read_text(encoding="utf-8"))
        lock_meta_path=bundle/"data_lock.json"
        source_lock_meta=json.loads(lock_meta_path.read_text(encoding="utf-8")) if lock_meta_path.exists() else {}
        prior_games = pd.read_csv(bundle/"games.csv", low_memory=False)
        row = prior_games[prior_games.game_id.astype(str).eq(game_id)]
        if len(row) != 1:
            raise ValueError(f"Original prediction bundle does not contain exactly one {game_id}")
        output.append({"game_id": game_id, "game_state_at_run": state.eligibility_state,
            "prediction_source_run_id": str(prior.selected_run_id), "prediction_generated_at": str(prior.run_cutoff_utc),
            "prediction_reused_immutable": True, "feature_lock_reference": str(source_meta.get("lock_id", "")),
            "pregame_refresh_reference": str(source_lock_meta.get("game_refresh_manifest", source_lock_meta.get("snapshot_id", source_meta.get("data_lock_snapshot_id", "")))),
            "game_prediction_sha256": hashlib.sha256(row.iloc[0].to_json(date_format="iso").encode("utf-8")).hexdigest()})
    result = pd.DataFrame(output)
    if len(result) != len(eligibility) or result.game_id.astype(str).duplicated().any() or fresh != set(generated_games.game_id.astype(str)):
        raise ValueError("Partial lineage does not cover the schedule exactly once")
    return result
