"""Research-only, point-in-time team-score feature construction.

The historical possession ledger is used *only* after a strict one-game/team
lag. Its current-game outcomes are never model inputs. This module does not
register a model or participate in production predictions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


WINDOW_GAMES = 8
PRIOR_DRIVES = 30.0
RATE_NUMERATORS = {
    "points_per_drive": "offensive_points",
    "td_per_drive": "td_possessions",
    "fg_per_drive": "fg_possessions",
    "turnover_per_drive": "turnover_possessions",
}
MATCHUP_METRICS = (
    "pass_epa", "rush_epa", "success_rate", "explosive_rate", "red_zone_td_rate",
)
DRIVE_FEATURES = tuple(
    f"{side}_{name}" for side in ("home", "away") for name in (
        "lag_off_possessions", "lag_def_allowed_possessions", "lag_plays_per_possession",
    )
) + ("pregame_pace_product",)
SCORING_FEATURES = tuple(
    f"{side}_lag_{orientation}_{metric}"
    for side in ("home", "away")
    for orientation in ("off", "def_allowed")
    for metric in RATE_NUMERATORS
)
MATCHUP_FEATURES = tuple(
    f"{side}_v3_matchup_{metric}_product"
    for side in ("home", "away") for metric in MATCHUP_METRICS
)
FORM_PREFIXES = ("home_pregame_", "away_pregame_", "home_matchup_", "away_matchup_")


def safe_phase2_features(registered_features: list[str]) -> list[str]:
    """Keep the baseline football context but reject unproven QB provenance.

    2022-24 starting QB snapshots are WEEKLY_ASSUMED_PREGAME, and the prior
    QB feature builder has a full-history median fallback for missing QB
    ratings. Neither is acceptable as a *new* PIT-safe candidate input.
    """
    rejected = [name for name in registered_features if "_qb_" in name or name.startswith("qb_")]
    safe = [name for name in registered_features if name not in rejected]
    if len(rejected) != 12:
        raise ValueError(f"Expected 12 historical QB fields, found {len(rejected)}")
    if any("target" in name or "actual" in name or "odds" in name for name in safe):
        raise ValueError("Unsafe registered feature in safe base")
    return safe


def _lagged_sum(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame.groupby("team", sort=False)[column].transform(
        lambda s: s.shift(1).rolling(WINDOW_GAMES, min_periods=1).sum()
    )


def _lagged_mean(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame.groupby("team", sort=False)[column].transform(
        lambda s: s.shift(1).rolling(WINDOW_GAMES, min_periods=1).mean()
    )


def build_lagged_drive_features(matrix: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Return exactly one game-keyed row of PIT-safe possession features.

    League priors are grouped by kickoff time and cumulatively shifted, so
    contemporaneous games cannot see one another's results. Team windows are
    shifted before rolling and continue across season boundaries.
    """
    if matrix.game_id.duplicated().any() or matrix.season.max() >= 2026:
        raise ValueError("Historical matrix must have unique 2022-25 game IDs")
    if set(matrix.season.unique()) - {2022, 2023, 2024, 2025}:
        raise ValueError("Unexpected historical season")
    games = matrix[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team"]].copy()
    games["kickoff_utc"] = pd.to_datetime(games.kickoff_utc, utc=True)
    home = games[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team"]].rename(
        columns={"home_team": "team", "away_team": "opponent"}).assign(side="home")
    away = games[["game_id", "season", "week", "kickoff_utc", "away_team", "home_team"]].rename(
        columns={"away_team": "team", "home_team": "opponent"}).assign(side="away")
    long = pd.concat([home, away], ignore_index=True)
    if long.duplicated(["game_id", "team"]).any() or long.duplicated(["team", "kickoff_utc"]).any():
        raise ValueError("Duplicate team-game or simultaneous same-team fixture")

    h = history.copy()
    h["kickoff_utc"] = pd.to_datetime(h.kickoff_utc, utc=True)
    if h.duplicated(["game_id", "team"]).any() or h.season.max() >= 2026:
        raise ValueError("Drive history has duplicate keys or 2026 outcomes")
    h = h[h.research_valid.fillna(False).astype(bool)].copy()
    h["offensive_points"] = (
        6 * h.offensive_td + h.pat_made + 2 * h.two_point_made + 3 * h.field_goals_made
    )
    if (h.offensive_possessions <= 0).any():
        raise ValueError("Nonpositive historical offensive possession count")
    for numerator in RATE_NUMERATORS.values():
        if (h[numerator] < 0).any():
            raise ValueError(f"Negative historical scoring component: {numerator}")
    observed = ["offensive_possessions", *RATE_NUMERATORS.values()]
    own = h[["game_id", "team", "kickoff_utc", *observed]].rename(
        columns={c: f"off_{c}" for c in observed})
    allowed = h[["game_id", "team", *observed]].rename(columns={
        "team": "opponent", **{c: f"def_allowed_{c}" for c in observed}
    })
    long = long.merge(own, on=["game_id", "team", "kickoff_utc"], how="left", validate="one_to_one")
    long = long.merge(allowed, on=["game_id", "opponent"], how="left", validate="one_to_one")
    long = long.sort_values(["team", "kickoff_utc", "game_id"]).reset_index(drop=True)

    # The prior at kickoff t includes only game groups with kickoff < t.
    league_by_kickoff = h.groupby("kickoff_utc", sort=True)[observed].sum().sort_index()
    league_prior = league_by_kickoff.cumsum().shift(1)
    for col in observed:
        long[f"_league_{col}"] = long.kickoff_utc.map(league_prior[col])
    long["last_prior_team_kickoff_utc"] = long.groupby("team", sort=False).kickoff_utc.shift(1)
    if ((long.last_prior_team_kickoff_utc.notna()) &
        (long.last_prior_team_kickoff_utc >= long.kickoff_utc)).any():
        raise ValueError("Team feature is not strictly pregame")

    for orientation in ("off", "def_allowed"):
        drives_col = f"{orientation}_offensive_possessions"
        long[f"lag_{orientation}_possession_sum"] = _lagged_sum(long, drives_col)
        long[f"lag_{orientation}_possessions"] = _lagged_mean(long, drives_col)
        for metric, numerator in RATE_NUMERATORS.items():
            count = _lagged_sum(long, f"{orientation}_{numerator}")
            league_rate = long[f"_league_{numerator}"] / long["_league_offensive_possessions"].replace(0, np.nan)
            prior_count = count.fillna(0)
            prior_drives = long[f"lag_{orientation}_possession_sum"].fillna(0)
            long[f"lag_{orientation}_{metric}"] = (
                prior_count + PRIOR_DRIVES * league_rate
            ) / (prior_drives + PRIOR_DRIVES)

    selected = ["game_id", "side", "last_prior_team_kickoff_utc",
                *[f"lag_{orientation}_possessions" for orientation in ("off", "def_allowed")],
                *[f"lag_{orientation}_{metric}" for orientation in ("off", "def_allowed") for metric in RATE_NUMERATORS]]
    home_features = long[long.side.eq("home")][selected].drop(columns="side").rename(
        columns={c: f"home_{c}" for c in selected if c != "game_id"})
    away_features = long[long.side.eq("away")][selected].drop(columns="side").rename(
        columns={c: f"away_{c}" for c in selected if c != "game_id"})
    out = games[["game_id"]].merge(home_features, on="game_id", validate="one_to_one")
    out = out.merge(away_features, on="game_id", validate="one_to_one")
    if len(out) != len(matrix):
        raise ValueError("Drive features lost games")
    return out


def build_v3_matrix(matrix: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    new = build_lagged_drive_features(matrix, history)
    out = matrix.merge(new, on="game_id", how="left", validate="one_to_one", sort=False)
    for side in ("home", "away"):
        other = "away" if side == "home" else "home"
        out[f"{side}_lag_plays_per_possession"] = (
            out[f"{side}_pregame_plays"] / out[f"{side}_lag_off_possessions"].replace(0, np.nan)
        )
        for metric in MATCHUP_METRICS:
            out[f"{side}_v3_matchup_{metric}_product"] = (
                out[f"{side}_pregame_{metric}"] * out[f"{other}_pregame_def_{metric}"]
            )
    out["pregame_pace_product"] = out.home_lag_off_possessions * out.away_lag_off_possessions
    expected = set(DRIVE_FEATURES + SCORING_FEATURES + MATCHUP_FEATURES)
    if not expected.issubset(out.columns):
        raise ValueError(f"Missing V3 feature columns: {sorted(expected - set(out.columns))}")
    return out
