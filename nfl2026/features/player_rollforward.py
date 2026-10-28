"""Point-in-time player usage feature roll-forward helpers.

The definitions here deliberately mirror ``build_advanced_player_matrix``:
last-eight player-game means, last-four snap-share means, last-six current-team
game means, and counts of prior actual player appearances. Candidate rows are
always evaluated strictly before kickoff.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PLAYER_USAGE_FIELDS = (
    "prior_carries", "prior_targets", "prior_rushing_yards", "prior_receiving_yards",
    "prior_anytime_offensive_td", "recent_red_zone_carries", "recent_inside_10_carries",
    "recent_inside_5_carries", "recent_red_zone_targets", "recent_inside_10_targets",
    "recent_inside_5_targets", "recent_snap_pct", "recent_team_carries", "recent_team_targets",
    "recent_yards_per_carry", "recent_yards_per_target", "player_games_before",
    "rookie_or_no_history",
)

ACTUAL_TO_FEATURE = {
    "carries": "prior_carries", "targets": "prior_targets", "rushing_yards": "prior_rushing_yards",
    "receiving_yards": "prior_receiving_yards", "anytime_offensive_td": "prior_anytime_offensive_td",
    "red_zone_carries": "recent_red_zone_carries", "inside_10_carries": "recent_inside_10_carries",
    "inside_5_carries": "recent_inside_5_carries", "red_zone_targets": "recent_red_zone_targets",
    "inside_10_targets": "recent_inside_10_targets", "inside_5_targets": "recent_inside_5_targets",
}

SKILL_POSITIONS = {"QB", "RB", "FB", "WR", "TE"}

PLAYER_HISTORY_KEYS = ("game_id", "player_id", "team", "kickoff_utc")
PLAYER_ACTIVITY_FIELDS = (
    "carries", "rushing_yards", "rushing_tds", "targets", "receptions",
    "receiving_yards", "air_yards", "receiving_tds", "anytime_offensive_tds",
    "anytime_offensive_td", "red_zone_carries", "inside_10_carries", "inside_5_carries",
    "red_zone_targets", "inside_10_targets", "inside_5_targets", "offense_pct",
)


def filter_unusable_player_observations(observations: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Drop only unkeyed zero-activity rows before strict player-state construction.

    Canonical postgame sources can contain a team-level/placeholder row with no
    player identity. Such a row cannot contribute player history. If it carries
    any meaningful player activity, fail closed rather than hiding the data.
    Missing optional feature values on otherwise keyed rows are not filtered.
    """
    missing_columns = set(PLAYER_HISTORY_KEYS) - set(observations.columns)
    if missing_columns:
        raise ValueError(f"Player observations missing {sorted(missing_columns)}")
    frame = observations.copy()
    invalid = frame[list(PLAYER_HISTORY_KEYS)].isna().any(axis=1)
    for column in PLAYER_HISTORY_KEYS:
        values = frame[column]
        if values.dtype == object or pd.api.types.is_string_dtype(values.dtype):
            invalid |= values.astype("string").str.strip().eq("").fillna(False)
    parsed_kickoff = pd.to_datetime(frame["kickoff_utc"], utc=True, errors="coerce")
    invalid |= parsed_kickoff.isna()

    activity_columns = [column for column in PLAYER_ACTIVITY_FIELDS if column in frame]
    active = pd.Series(False, index=frame.index)
    for column in activity_columns:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        # Non-numeric activity data is not safe to classify as a zero row.
        non_numeric = frame[column].notna() & numeric.isna()
        if (invalid & non_numeric).any():
            count = int((invalid & non_numeric).sum())
            raise ValueError(f"{count} unkeyed player observations have unclassifiable {column} activity")
        active |= numeric.fillna(0).ne(0)
    unidentified_nonzero = invalid & active
    if unidentified_nonzero.any():
        columns=[column for column in ("season","week",*PLAYER_HISTORY_KEYS,*activity_columns) if column in frame]
        examples=frame.loc[unidentified_nonzero,columns].head(5).to_dict(orient="records")
        raise ValueError(
            f"{int(unidentified_nonzero.sum())} unkeyed player observations contain nonzero player statistics; "
            f"examples={examples}"
        )

    excluded = invalid & ~active
    usable = frame.loc[~excluded].copy()
    diagnostic = {
        "player_observations_loaded": int(len(frame)),
        "player_observations_usable": int(len(usable)),
        "unidentified_zero_usage_rows_excluded": int((excluded & (
            frame.player_id.isna() | frame.player_id.astype("string").str.strip().eq("").fillna(False)
        )).sum()),
        "unkeyed_zero_usage_rows_excluded": int(excluded.sum()),
        "unidentified_nonzero_rows_blocked": 0,
    }
    return usable, diagnostic


def roll_player_usage_features(observations: pd.DataFrame, candidates: pd.DataFrame, *,
                               snap_source_available: bool = True,
                               player_window: int = 8, snap_window: int = 4,
                               team_window: int = 6) -> pd.DataFrame:
    """Return candidates with training-parity recent features from prior actual games.

    ``observations`` contains actual player-game rows and event-derived red-zone
    fields. Team volume is aggregated by actual game and team, so a player's
    personal history follows their ID while team context follows candidate.team.
    Missing source-level snap data remains NA (the trained pipeline imputer owns
    the fallback); a missing snap row in an otherwise available source is zero,
    matching historical training preprocessing.
    """
    if player_window < 1 or snap_window < 1 or team_window < 1:
        raise ValueError("Rolling windows must be positive")
    required_obs = {"game_id", "player_id", "team", "kickoff_utc", "carries", "targets",
                    "rushing_yards", "receiving_yards", "anytime_offensive_td"}
    required_candidates = {"game_id", "player_id", "team", "position", "kickoff_utc"}
    if not required_obs.issubset(observations):
        raise ValueError(f"Player observations missing {sorted(required_obs-set(observations))}")
    if not required_candidates.issubset(candidates):
        raise ValueError(f"Player candidates missing {sorted(required_candidates-set(candidates))}")
    obs = observations.copy()
    cand = candidates.copy()
    obs["kickoff_utc"] = pd.to_datetime(obs.kickoff_utc, utc=True, errors="coerce")
    cand["kickoff_utc"] = pd.to_datetime(cand.kickoff_utc, utc=True, errors="coerce")
    if obs[["game_id", "player_id", "team", "kickoff_utc"]].isna().any().any():
        raise ValueError("Player observations contain incomplete game/player/team/kickoff keys")
    if cand[["game_id", "player_id", "team", "kickoff_utc"]].isna().any().any():
        raise ValueError("Player candidates contain incomplete game/player/team/kickoff keys")
    if obs.duplicated(["game_id", "player_id"]).any():
        raise ValueError("Duplicate player-game observations")
    if cand.duplicated(["game_id", "player_id"]).any():
        raise ValueError("Duplicate player-game candidates")
    # The historical Phase-2 conditional matrix is position-unfiltered. Keep
    # that full population for position priors and team volume, then restrict
    # only the player-specific state and output candidate population.
    full_observations = obs.copy()
    obs = obs[obs.position.astype(str).str.upper().isin(SKILL_POSITIONS)].copy() if "position" in obs else obs
    cand = cand[cand.position.astype(str).str.upper().isin(SKILL_POSITIONS)].copy()
    cand = cand.drop(columns=list(PLAYER_USAGE_FIELDS), errors="ignore")

    actual_cols = ["carries", "targets", "rushing_yards", "receiving_yards", "anytime_offensive_td"]
    red_cols = ["red_zone_carries", "inside_10_carries", "inside_5_carries",
                "red_zone_targets", "inside_10_targets", "inside_5_targets"]
    for col in actual_cols + red_cols:
        if col not in obs:
            obs[col] = 0.0
        obs[col] = pd.to_numeric(obs[col], errors="coerce").fillna(0.0)
    if "offense_pct" not in obs:
        obs["offense_pct"] = np.nan
    obs["offense_pct"] = pd.to_numeric(obs.offense_pct, errors="coerce")
    if not snap_source_available:
        obs["offense_pct"] = np.nan

    # Build state after each observation, then perform a vectorized as-of join.
    # Adding one nanosecond to the source event time and disallowing exact
    # matches excludes the target game even when source and target kickoffs
    # coincide. This is the same state timing used by the training builder.
    team_observations = full_observations.copy()
    obs["player_id"] = obs.player_id.astype(str)
    cand["player_id"] = cand.player_id.astype(str)
    obs["team"] = obs.team.astype(str)
    cand["team"] = cand.team.astype(str)
    cand["_player_rollforward_order"] = np.arange(len(cand))
    obs = obs.sort_values(["player_id", "kickoff_utc", "game_id"], kind="mergesort").copy()
    state = obs[["player_id", "kickoff_utc", "game_id", *actual_cols, *red_cols, "offense_pct"]].copy()
    feature_sources = {**ACTUAL_TO_FEATURE, **{c: ACTUAL_TO_FEATURE[c] for c in red_cols}}
    for source, target in feature_sources.items():
        state[target] = state.groupby("player_id", sort=False)[source].transform(
            lambda values: values.rolling(player_window, min_periods=1).mean())
    state["recent_snap_pct"] = state.groupby("player_id", sort=False).offense_pct.transform(
        lambda values: values.rolling(snap_window, min_periods=1).mean())
    state["player_games_before"] = state.groupby("player_id", sort=False).cumcount() + 1
    state["rookie_or_no_history"] = 0.0
    state["effective_utc"] = state.kickoff_utc + pd.Timedelta(nanoseconds=1)
    player_state_fields = ["player_id", "effective_utc", *dict.fromkeys(feature_sources.values()),
                           "recent_snap_pct", "player_games_before", "rookie_or_no_history"]
    output = cand.sort_values(["kickoff_utc", "player_id", "game_id"], kind="mergesort").copy()
    output = pd.merge_asof(output, state[player_state_fields].sort_values(["effective_utc", "player_id"], kind="mergesort"),
        left_on="kickoff_utc", right_on="effective_utc", by="player_id", direction="backward", allow_exact_matches=False)
    output["player_games_before"] = pd.to_numeric(output.player_games_before, errors="coerce").fillna(0)
    output["rookie_or_no_history"] = output.player_games_before.eq(0).astype(float)

    # Keep no-history player fields missing here. The production candidate
    # builder owns the established position-median fallback from the frozen
    # historical training matrix, followed by the registered estimator's
    # median imputer if a position has no stored fallback. The conditional
    # participant matrix has a distinct expanding position-prior fallback;
    # using that here would silently change the inference-population contract.

    # The training matrix's team totals were summed before the five-position
    # candidate filter; retain that exact source population for team context.
    team_observations["team"] = team_observations.team.astype(str)
    team_game = team_observations.groupby(["game_id", "team", "kickoff_utc"], as_index=False).agg(
        team_carries=("carries", "sum"), team_targets=("targets", "sum"))
    team_game = team_game.sort_values(["team", "kickoff_utc", "game_id"], kind="mergesort").copy()
    # Training rows use shift(1).rolling(window); to produce a target row as an
    # as-of feature, persist each completed game's *inclusive trailing window*
    # and expose it only after that game's kickoff. This avoids the historical
    # candidate path's extra one-game lag.
    team_game["recent_team_carries"] = team_game.groupby("team", sort=False).team_carries.transform(
        lambda values: values.rolling(team_window, min_periods=1).mean())
    team_game["recent_team_targets"] = team_game.groupby("team", sort=False).team_targets.transform(
        lambda values: values.rolling(team_window, min_periods=1).mean())
    team_game["effective_utc"] = team_game.kickoff_utc + pd.Timedelta(nanoseconds=1)
    output = pd.merge_asof(output.sort_values(["kickoff_utc", "team", "player_id"], kind="mergesort"),
        team_game[["team", "effective_utc", "recent_team_carries", "recent_team_targets"]].sort_values(
            ["effective_utc", "team"], kind="mergesort"), left_on="kickoff_utc", right_on="effective_utc",
        by="team", direction="backward", allow_exact_matches=False)
    if not snap_source_available:
        output["recent_snap_pct"] = np.nan
    output["recent_yards_per_carry"] = output.prior_rushing_yards / output.prior_carries.replace(0, np.nan)
    output["recent_yards_per_target"] = output.prior_receiving_yards / output.prior_targets.replace(0, np.nan)
    return output.sort_values("_player_rollforward_order", kind="mergesort").drop(
        columns="_player_rollforward_order").reset_index(drop=True)


def aggregate_player_red_zone(raw: pd.DataFrame) -> pd.DataFrame:
    """Aggregate six PBP event counts exactly as the historical canonical builder."""
    required = {"game_id", "posteam", "defteam", "yardline_100", "rush_attempt", "pass_attempt",
                "qb_kneel", "qb_spike", "receiver_player_id", "rusher_player_id", "season_type"}
    if not required.issubset(raw):
        raise ValueError(f"PBP lacks {sorted(required-set(raw))}")
    frame = raw[raw.season_type.astype(str).str.upper().eq("REG")].copy()
    for col in ("yardline_100", "rush_attempt", "pass_attempt", "qb_kneel", "qb_spike"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0)
    rec = frame[frame.receiver_player_id.notna() & frame.pass_attempt.eq(1)].copy()
    rec["player_id"] = rec.receiver_player_id.astype(str)
    rec["team"] = rec.posteam
    rush = frame[frame.rusher_player_id.notna()].copy()
    valid = rush.rush_attempt.eq(1) & rush.qb_kneel.ne(1) & rush.qb_spike.ne(1)
    rush = rush[valid].copy()
    rush["player_id"] = rush.rusher_player_id.astype(str)
    rush["team"] = rush.posteam
    pieces = []
    for source, kind in ((rush, "carries"), (rec, "targets")):
        if source.empty:
            continue
        source[f"red_zone_{kind}"] = source.yardline_100.between(1, 20).astype(int)
        source[f"inside_10_{kind}"] = source.yardline_100.between(1, 10).astype(int)
        source[f"inside_5_{kind}"] = source.yardline_100.between(1, 5).astype(int)
        cols = [f"red_zone_{kind}", f"inside_10_{kind}", f"inside_5_{kind}"]
        pieces.append(source.groupby(["game_id", "player_id", "team"], as_index=False)[cols].sum())
    keys = ["game_id", "player_id", "team"]
    if not pieces:
        return pd.DataFrame(columns=keys + ["red_zone_carries", "inside_10_carries", "inside_5_carries",
                                            "red_zone_targets", "inside_10_targets", "inside_5_targets"])
    out = pieces[0]
    for part in pieces[1:]:
        out = out.merge(part, on=keys, how="outer", validate="one_to_one")
    value_cols = ["red_zone_carries", "inside_10_carries", "inside_5_carries",
                  "red_zone_targets", "inside_10_targets", "inside_5_targets"]
    for col in value_cols:
        if col not in out:
            out[col] = 0
    out[value_cols] = out[value_cols].fillna(0).astype(int)
    return out
