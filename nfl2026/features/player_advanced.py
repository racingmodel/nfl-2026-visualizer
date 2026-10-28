from __future__ import annotations

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.utilities.io import write_csv_json


PLAYER_FEATURES = [
    "prior_carries", "prior_targets", "prior_rushing_yards", "prior_receiving_yards", "prior_anytime_offensive_td",
    "recent_red_zone_carries", "recent_inside_10_carries", "recent_inside_5_carries", "recent_red_zone_targets",
    "recent_inside_10_targets", "recent_inside_5_targets", "recent_snap_pct", "recent_team_carries", "recent_team_targets",
    "recent_yards_per_carry", "recent_yards_per_target", "player_games_before", "rookie_or_no_history",
    "position_QB", "position_RB", "position_FB", "position_WR", "position_TE",
]


def _shift(frame: pd.DataFrame, column: str, window: int) -> pd.Series:
    ordered = frame.sort_values(["player_id", "kickoff_utc", "game_id"])
    value = ordered.groupby("player_id", sort=False)[column].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
    return value.reindex(frame.index)


def build_advanced_player_matrix(
    settings: Settings,
    phase1_matrix: pd.DataFrame,
    player_pbp: pd.DataFrame,
    snap_counts: pd.DataFrame,
    universe: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    matrix = phase1_matrix.copy()
    matrix["kickoff_utc"] = pd.to_datetime(matrix["prediction_cutoff_utc"], utc=True)
    extra_cols = ["red_zone_carries", "inside_10_carries", "inside_5_carries", "red_zone_targets", "inside_10_targets", "inside_5_targets"]
    extra = player_pbp[["game_id", "player_id", *extra_cols]].drop_duplicates(["game_id", "player_id"])
    matrix = matrix.merge(extra, on=["game_id", "player_id"], how="left")
    snap = snap_counts[["game_id", "player_id", "offense_pct"]].dropna(subset=["player_id"]).drop_duplicates(["game_id", "player_id"])
    matrix = matrix.merge(snap, on=["game_id", "player_id"], how="left")
    for col in extra_cols:
        matrix[col] = matrix[col].fillna(0)
        matrix[f"recent_{col}"] = _shift(matrix, col, settings.phase2_player_history_window)
    matrix["recent_snap_pct"] = _shift(matrix, "offense_pct", 4)
    team_week = matrix.groupby(["game_id", "team"], as_index=False).agg(team_carries=("target_carries", "sum"), team_targets=("target_targets", "sum"))
    fixture = matrix[["game_id", "team", "kickoff_utc"]].drop_duplicates()
    team_week = team_week.merge(fixture, on=["game_id", "team"])
    team_week = team_week.sort_values(["team", "kickoff_utc", "game_id"])
    team_week["recent_team_carries"] = team_week.groupby("team")["team_carries"].transform(lambda s: s.shift(1).rolling(6, min_periods=1).mean())
    team_week["recent_team_targets"] = team_week.groupby("team")["team_targets"].transform(lambda s: s.shift(1).rolling(6, min_periods=1).mean())
    matrix = matrix.merge(team_week[["game_id", "team", "recent_team_carries", "recent_team_targets"]], on=["game_id", "team"], how="left")
    matrix["recent_yards_per_carry"] = matrix["prior_rushing_yards"] / matrix["prior_carries"].replace(0, np.nan)
    matrix["recent_yards_per_target"] = matrix["prior_receiving_yards"] / matrix["prior_targets"].replace(0, np.nan)
    for pos in ("QB", "RB", "FB", "WR", "TE"):
        matrix[f"position_{pos}"] = matrix["position"].eq(pos).astype(float)
    roster_key = universe[["game_id", "player_id", "status", "universe_reliability"]].drop_duplicates()
    matrix = matrix.merge(roster_key, on=["game_id", "player_id"], how="left")
    matrix["in_pregame_universe"] = matrix["status"].eq("ACT")
    matrix["roster_reliability"] = matrix["universe_reliability"].fillna("NOT_AVAILABLE")
    matrix["feature_version"] = settings.phase2_feature_version

    # Build an as-of state table after every observed game. It lets nonparticipants receive predictions without
    # copying their current-game targets into the features.
    observed = matrix.sort_values(["player_id", "kickoff_utc", "game_id"]).copy()
    state_columns = ["target_carries", "target_targets", "target_rushing_yards", "target_receiving_yards", "target_anytime_offensive_td", *extra_cols, "offense_pct"]
    for col in state_columns:
        observed[col] = pd.to_numeric(observed[col], errors="coerce").fillna(0)
    state = observed[["player_id", "kickoff_utc", "position", *state_columns]].copy()
    for col, output in [
        ("target_carries", "prior_carries"), ("target_targets", "prior_targets"), ("target_rushing_yards", "prior_rushing_yards"),
        ("target_receiving_yards", "prior_receiving_yards"), ("target_anytime_offensive_td", "prior_anytime_offensive_td"),
        ("red_zone_carries", "recent_red_zone_carries"), ("inside_10_carries", "recent_inside_10_carries"),
        ("inside_5_carries", "recent_inside_5_carries"), ("red_zone_targets", "recent_red_zone_targets"),
        ("inside_10_targets", "recent_inside_10_targets"), ("inside_5_targets", "recent_inside_5_targets"),
        ("offense_pct", "recent_snap_pct"),
    ]:
        state[output] = state.groupby("player_id")[col].transform(lambda s: s.rolling(settings.phase2_player_history_window if col != "offense_pct" else 4, min_periods=1).mean())
    state["player_games_before"] = state.groupby("player_id").cumcount() + 1
    state["effective_utc"] = state["kickoff_utc"] + pd.Timedelta(nanoseconds=1)
    keep_state = ["player_id", "effective_utc", "position", *[c for c in PLAYER_FEATURES if not c.startswith("position_") and c not in {"recent_team_carries", "recent_team_targets", "rookie_or_no_history", "recent_yards_per_carry", "recent_yards_per_target"}]]
    state = state[keep_state].sort_values(["effective_utc", "player_id"])
    candidates = universe[universe["status_eligible"]].copy()
    candidates["kickoff_utc"] = pd.to_datetime(candidates["kickoff_utc"], utc=True)
    candidates = candidates.sort_values(["kickoff_utc", "player_id"])
    candidate_parts = []
    for player_id, group in candidates.groupby("player_id", sort=False):
        hist = state[state.player_id.eq(player_id)].sort_values("effective_utc")
        group = group.sort_values("kickoff_utc")
        if hist.empty:
            merged = group.copy()
            for col in keep_state[3:]:
                merged[col] = np.nan
        else:
            merged = pd.merge_asof(group, hist.drop(columns=["player_id", "position"]), left_on="kickoff_utc", right_on="effective_utc", direction="backward", allow_exact_matches=False)
        candidate_parts.append(merged)
    candidates = pd.concat(candidate_parts, ignore_index=True)
    candidates["player_games_before"] = candidates["player_games_before"].fillna(0)
    candidates["rookie_or_no_history"] = candidates["player_games_before"].eq(0).astype(float)
    pos_defaults = matrix.groupby("position")[["prior_carries", "prior_targets", "prior_rushing_yards", "prior_receiving_yards", "prior_anytime_offensive_td", "recent_yards_per_carry", "recent_yards_per_target"]].median()
    for col in ("prior_carries", "prior_targets", "prior_rushing_yards", "prior_receiving_yards", "prior_anytime_offensive_td"):
        candidates[col] = candidates[col].fillna(candidates["position"].map(pos_defaults[col])).fillna(0)
    candidates["recent_yards_per_carry"] = candidates["prior_rushing_yards"] / candidates["prior_carries"].replace(0, np.nan)
    candidates["recent_yards_per_target"] = candidates["prior_receiving_yards"] / candidates["prior_targets"].replace(0, np.nan)
    team_state = team_week[["team", "kickoff_utc", "recent_team_carries", "recent_team_targets"]].drop_duplicates().sort_values(["kickoff_utc", "team"])
    cand_team_parts = []
    for team, group in candidates.groupby("team", sort=False):
        history = team_state[team_state.team.eq(team)].drop(columns="team").sort_values("kickoff_utc")
        group = group.sort_values("kickoff_utc")
        cand_team_parts.append(pd.merge_asof(group, history, on="kickoff_utc", direction="backward", allow_exact_matches=True) if not history.empty else group)
    candidates = pd.concat(cand_team_parts, ignore_index=True)
    for pos in ("QB", "RB", "FB", "WR", "TE"):
        candidates[f"position_{pos}"] = candidates["position"].eq(pos).astype(float)
    candidates["feature_version"] = settings.phase2_feature_version
    candidates["evaluation_population"] = "true_pregame_roster_universe"
    matrix["evaluation_population"] = "actual_week_participants_conditional"
    write_csv_json(matrix, settings.artifact_root / "phase2" / "player_advanced_conditional_matrix.csv")
    write_csv_json(candidates, settings.artifact_root / "phase2" / "player_pregame_universe_features.csv")
    return matrix, candidates, PLAYER_FEATURES
