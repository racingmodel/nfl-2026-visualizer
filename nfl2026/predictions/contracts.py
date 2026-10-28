from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from nfl2026.models.probability import clip_probability


TEAM_PREDICTION_COLUMNS = [
    "game_id", "season", "week", "kickoff_utc", "prediction_timestamp_utc", "away_team", "home_team",
    "away_expected_points", "home_expected_points", "model_margin", "model_total", "home_win_probability",
    "away_win_probability", "model_confidence",
]
PLAYER_PREDICTION_COLUMNS = [
    "game_id", "player_id", "player_name", "team", "position", "td_probability", "expected_carries",
    "expected_targets", "expected_rush_yards", "expected_receiving_yards",
]
PHASE2_TEAM_PREDICTION_COLUMNS = [
    "game_id", "season", "week", "kickoff_utc", "prediction_timestamp_utc", "data_cutoff_utc", "away_team", "home_team",
    "away_expected_points", "home_expected_points", "model_margin", "model_total", "home_win_probability", "away_win_probability",
    "home_score_p10", "home_score_p25", "home_score_p50", "home_score_p75", "home_score_p90",
    "away_score_p10", "away_score_p25", "away_score_p50", "away_score_p75", "away_score_p90",
    "margin_p10", "margin_p25", "margin_p50", "margin_p75", "margin_p90", "total_p10", "total_p25", "total_p50", "total_p75", "total_p90",
    "simulation_draws", "model_name", "model_version", "feature_version", "model_confidence", "confidence_basis", "home_qb_id", "away_qb_id", "qb_reliability", "explanation_json",
]
PHASE2_PLAYER_PREDICTION_COLUMNS = [
    "game_id", "season", "week", "kickoff_utc", "prediction_timestamp_utc", "data_cutoff_utc", "player_id", "player_name", "team", "opponent", "position",
    "roster_status", "universe_reliability", "expected_carries", "carries_p10", "carries_p50", "carries_p90", "expected_targets", "targets_p10", "targets_p50", "targets_p90",
    "expected_rush_yards", "rush_yards_p10", "rush_yards_p50", "rush_yards_p90", "expected_receiving_yards", "receiving_yards_p10", "receiving_yards_p50", "receiving_yards_p90",
    "raw_td_probability", "reconciled_td_probability", "model_version", "feature_version", "model_confidence", "confidence_basis", "explanation_json",
]
ACTUAL_COLUMNS = ["game_id", "kickoff_utc", "away_team", "home_team", "away_score", "home_score", "actual_source"]


def validate_team_prediction_schema(frame: pd.DataFrame) -> None:
    missing = sorted(set(TEAM_PREDICTION_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing team prediction fields: {missing}")
    for column in ("home_win_probability", "away_win_probability", "model_confidence"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or (~values.between(0, 1)).any():
            raise ValueError(f"Invalid probability field: {column}")


def write_immutable_bundle(
    root: Path,
    season: int,
    week: int,
    run_id: str,
    team_predictions: pd.DataFrame,
    player_predictions: pd.DataFrame,
    metadata: dict[str, Any],
) -> Path:
    destination = root / str(season) / f"week_{week:02d}" / run_id
    if destination.exists():
        raise FileExistsError(f"Prediction bundle already exists: {destination}")
    validate_team_prediction_schema(team_predictions)
    missing_player = sorted(set(PLAYER_PREDICTION_COLUMNS) - set(player_predictions.columns))
    if missing_player:
        raise ValueError(f"Missing player prediction fields: {missing_player}")
    destination.mkdir(parents=True)
    team_predictions[TEAM_PREDICTION_COLUMNS].to_csv(destination / "team_predictions.csv", index=False, lineterminator="\n")
    player_predictions[PLAYER_PREDICTION_COLUMNS].to_csv(destination / "player_predictions.csv", index=False, lineterminator="\n")
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def write_phase2_bundle(root: Path, season: int, week: int, run_id: str, team_predictions: pd.DataFrame, player_predictions: pd.DataFrame, metadata: dict[str, Any]) -> Path:
    destination = root / str(season) / f"week_{week:02d}" / run_id
    if destination.exists():
        raise FileExistsError(f"Prediction bundle already exists: {destination}")
    for label, frame, required in (("team", team_predictions, PHASE2_TEAM_PREDICTION_COLUMNS), ("player", player_predictions, PHASE2_PLAYER_PREDICTION_COLUMNS)):
        missing = sorted(set(required) - set(frame.columns))
        if missing:
            raise ValueError(f"Missing Phase 2 {label} prediction fields: {missing}")
    for column in ("home_win_probability", "away_win_probability", "model_confidence"):
        if not pd.to_numeric(team_predictions[column], errors="coerce").between(0, 1).all():
            raise ValueError(f"Invalid Phase 2 probability: {column}")
    destination.mkdir(parents=True)
    team_predictions[PHASE2_TEAM_PREDICTION_COLUMNS].to_csv(destination / "team_predictions.csv", index=False, lineterminator="\n")
    player_predictions[PHASE2_PLAYER_PREDICTION_COLUMNS].to_csv(destination / "player_predictions.csv", index=False, lineterminator="\n")
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def evaluate_team_predictions(predictions: pd.DataFrame, actuals: pd.DataFrame) -> pd.DataFrame:
    if "actual_source" not in actuals or actuals["actual_source"].isna().any():
        raise ValueError("Actual results require an explicit independent actual_source")
    forbidden_actuals = {"home_expected_points", "away_expected_points"} & set(actuals.columns)
    if forbidden_actuals:
        raise ValueError("Actuals cannot contain prediction fields")
    validate_team_prediction_schema(predictions)
    joined = predictions.merge(actuals[ACTUAL_COLUMNS], on="game_id", suffixes=("_prediction", "_actual"), validate="one_to_one")
    prediction_time = pd.to_datetime(joined["prediction_timestamp_utc"], utc=True)
    kickoff = pd.to_datetime(joined["kickoff_utc_actual"], utc=True)
    if (prediction_time >= kickoff).any():
        raise ValueError("Predictions must be timestamped before kickoff")
    joined["home_error"] = joined["home_expected_points"] - joined["home_score"]
    joined["away_error"] = joined["away_expected_points"] - joined["away_score"]
    return joined


def historical_bundle_frames(
    team_backtest: pd.DataFrame,
    player_backtest: pd.DataFrame,
    season: int,
    week: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    team = team_backtest[(team_backtest["season"].eq(season)) & (team_backtest["week"].eq(week)) & (team_backtest["model"].eq("recent_points_for_against"))].copy()
    if team.empty:
        raise ValueError("No team backtest rows for immutable example bundle")
    prediction_time = pd.to_datetime(team["kickoff_utc"], utc=True) - pd.Timedelta(days=1)
    margin = team["pred_home_score"] - team["pred_away_score"]
    home_probability = clip_probability(1 / (1 + np.exp(-margin / 7.5)))
    team_out = pd.DataFrame({
        "game_id": team["game_id"], "season": season, "week": week, "kickoff_utc": team["kickoff_utc"],
        "prediction_timestamp_utc": prediction_time, "away_team": team["away_team"], "home_team": team["home_team"],
        "away_expected_points": team["pred_away_score"], "home_expected_points": team["pred_home_score"],
        "model_margin": margin, "model_total": team["pred_home_score"] + team["pred_away_score"],
        "home_win_probability": home_probability, "away_win_probability": 1 - home_probability,
        "model_confidence": np.minimum(np.abs(home_probability - 0.5) * 2, 1),
    })
    players = player_backtest[(player_backtest["season"].eq(season)) & (player_backtest["week"].eq(week))].copy()
    player_out = pd.DataFrame({
        "game_id": players["game_id"], "player_id": players["player_id"], "player_name": players["display_name"],
        "team": players["team"], "position": players["position"], "td_probability": players["pred_td_probability"],
        "expected_carries": players["pred_carries"], "expected_targets": players["pred_targets"],
        "expected_rush_yards": players["pred_rushing_yards"], "expected_receiving_yards": players["pred_receiving_yards"],
    })
    return team_out, player_out
