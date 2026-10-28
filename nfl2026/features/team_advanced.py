from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.utilities.io import write_csv_json
from nfl2026.features.quarterback import augment_with_qb_features


TEAM_METRICS = [
    "points_for", "points_against", "plays", "drives", "epa_per_play", "success_rate", "early_down_success",
    "explosive_rate", "yards_per_play", "red_zone_td_rate", "third_down_success", "pass_rate", "neutral_pass_rate",
    "turnover_rate", "sack_rate", "offensive_tds", "pass_epa", "rush_epa", "pressure_rate",
]


def _pbp_path(settings: Settings, season: int) -> Path:
    return settings.legacy_root / f"{season} Stats" / f"pbp_{season}.parquet"


def aggregate_team_pbp(settings: Settings, seasons=None) -> pd.DataFrame:
    columns = [
        "game_id", "season", "season_type", "week", "posteam", "defteam", "play_type", "epa", "success", "yards_gained",
        "down", "ydstogo", "yardline_100", "fixed_drive", "rush_attempt", "pass_attempt", "sack", "interception",
        "fumble_lost", "qb_hit", "qb_kneel", "qb_spike", "touchdown", "pass_touchdown", "rush_touchdown",
    ]
    frames = []
    for season in (settings.training_seasons if seasons is None else seasons):
        raw = pd.read_parquet(_pbp_path(settings, season), columns=columns)
        raw = raw[raw["season_type"].eq("REG") & raw["posteam"].notna()].copy()
        numeric = [c for c in columns if c not in {"game_id", "season_type", "posteam", "defteam", "play_type"}]
        raw[numeric] = raw[numeric].apply(pd.to_numeric, errors="coerce")
        valid = raw["play_type"].isin(["run", "pass"]) & raw["epa"].notna() & raw["qb_kneel"].fillna(0).ne(1) & raw["qb_spike"].fillna(0).ne(1)
        raw = raw[valid].copy()
        raw["play"] = 1
        raw["drive_key"] = raw["fixed_drive"]
        raw["early_success"] = (raw["down"].le(2) & raw["success"].eq(1)).astype(float)
        raw["early_play"] = raw["down"].le(2).astype(float)
        raw["explosive"] = raw["yards_gained"].ge(20).astype(float)
        raw["third_success"] = (raw["down"].eq(3) & raw["success"].eq(1)).astype(float)
        raw["third_play"] = raw["down"].eq(3).astype(float)
        raw["neutral_pass"] = (raw["pass_attempt"].fillna(0).eq(1) & raw["down"].le(2) & raw["yardline_100"].between(20, 80)).astype(float)
        raw["neutral_play"] = (raw["down"].le(2) & raw["yardline_100"].between(20, 80)).astype(float)
        raw["turnover"] = raw["interception"].fillna(0) + raw["fumble_lost"].fillna(0)
        raw["offensive_td"] = (raw["pass_touchdown"].fillna(0) + raw["rush_touchdown"].fillna(0)).clip(0, 1)
        raw["rz_play"] = raw["yardline_100"].between(1, 20).astype(float)
        raw["rz_td"] = (raw["rz_play"].eq(1) & raw["offensive_td"].eq(1)).astype(float)
        raw["pass_epa_num"] = raw["epa"].where(raw["pass_attempt"].fillna(0).eq(1), 0)
        raw["pass_n"] = raw["pass_attempt"].fillna(0)
        raw["rush_epa_num"] = raw["epa"].where(raw["rush_attempt"].fillna(0).eq(1), 0)
        raw["rush_n"] = raw["rush_attempt"].fillna(0)
        agg = raw.groupby(["game_id", "season", "week", "posteam", "defteam"], as_index=False).agg(
            plays=("play", "sum"), drives=("drive_key", "nunique"), epa_sum=("epa", "sum"), successes=("success", "sum"),
            early_successes=("early_success", "sum"), early_plays=("early_play", "sum"), explosives=("explosive", "sum"),
            yards=("yards_gained", "sum"), rz_tds=("rz_td", "sum"), rz_plays=("rz_play", "sum"),
            third_successes=("third_success", "sum"), third_plays=("third_play", "sum"), passes=("pass_attempt", "sum"),
            neutral_passes=("neutral_pass", "sum"), neutral_plays=("neutral_play", "sum"), turnovers=("turnover", "sum"),
            sacks=("sack", "sum"), qb_hits=("qb_hit", "sum"), offensive_tds=("offensive_td", "sum"), pass_epa_sum=("pass_epa_num", "sum"),
            pass_n=("pass_n", "sum"), rush_epa_sum=("rush_epa_num", "sum"), rush_n=("rush_n", "sum"),
        )
        frames.append(agg)
    out = pd.concat(frames, ignore_index=True)
    denominator = lambda x: x.replace(0, np.nan)
    out["epa_per_play"] = out["epa_sum"] / denominator(out["plays"])
    out["success_rate"] = out["successes"] / denominator(out["plays"])
    out["early_down_success"] = out["early_successes"] / denominator(out["early_plays"])
    out["explosive_rate"] = out["explosives"] / denominator(out["plays"])
    out["yards_per_play"] = out["yards"] / denominator(out["plays"])
    out["red_zone_td_rate"] = out["rz_tds"] / denominator(out["rz_plays"])
    out["third_down_success"] = out["third_successes"] / denominator(out["third_plays"])
    out["pass_rate"] = out["passes"] / denominator(out["plays"])
    out["neutral_pass_rate"] = out["neutral_passes"] / denominator(out["neutral_plays"])
    out["turnover_rate"] = out["turnovers"] / denominator(out["plays"])
    out["sack_rate"] = out["sacks"] / denominator(out["passes"])
    out["pass_epa"] = out["pass_epa_sum"] / denominator(out["pass_n"])
    out["rush_epa"] = out["rush_epa_sum"] / denominator(out["rush_n"])
    out["pressure_rate"] = (out["qb_hits"] + out["sacks"]) / denominator(out["passes"])
    return out.rename(columns={"posteam": "team", "defteam": "opponent"})


def _shifted_rolling(frame: pd.DataFrame, group: str, column: str, window: int) -> pd.Series:
    ordered = frame.sort_values([group, "kickoff_utc", "game_id"])
    rolled = ordered.groupby(group, sort=False)[column].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
    return rolled.reindex(frame.index)


def _elo_features(games: pd.DataFrame) -> pd.DataFrame:
    elo = defaultdict(lambda: 1500.0)
    rows = []
    for kickoff, block in games.sort_values(["kickoff_utc", "game_id"]).groupby("kickoff_utc", sort=True):
        for game in block.itertuples(index=False):
            rows.append({"game_id": game.game_id, "home_elo": elo[game.home_team], "away_elo": elo[game.away_team]})
        for game in block.itertuples(index=False):
            exp = 1 / (1 + 10 ** (-(elo[game.home_team] + 45 - elo[game.away_team]) / 400))
            actual = 1.0 if game.home_score > game.away_score else (0.5 if game.home_score == game.away_score else 0.0)
            change = 20 * (actual - exp)
            elo[game.home_team] += change
            elo[game.away_team] -= change
    return pd.DataFrame(rows)


def build_advanced_team_matrix(settings: Settings, games: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    played = games[games["result_status"].eq("completed") & games["game_type"].eq("REG")].copy()
    pbp = aggregate_team_pbp(settings)
    long = pd.concat([
        played[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team", "home_score", "away_score"]].rename(columns={"home_team": "team", "away_team": "opponent", "home_score": "points_for", "away_score": "points_against"}).assign(is_home=1),
        played[["game_id", "season", "week", "kickoff_utc", "away_team", "home_team", "away_score", "home_score"]].rename(columns={"away_team": "team", "home_team": "opponent", "away_score": "points_for", "home_score": "points_against"}).assign(is_home=0),
    ], ignore_index=True).merge(pbp, on=["game_id", "season", "week", "team", "opponent"], how="left")
    defense = pbp[["game_id", "opponent", *[c for c in TEAM_METRICS if c not in {"points_for", "points_against"}]]].rename(columns={"opponent": "team", **{c: f"def_{c}" for c in TEAM_METRICS if c not in {"points_for", "points_against"}}})
    long = long.merge(defense, on=["game_id", "team"], how="left")
    roll_cols = [c for c in TEAM_METRICS if c in long] + [f"def_{c}" for c in TEAM_METRICS if f"def_{c}" in long]
    for column in roll_cols:
        long[f"pregame_{column}"] = _shifted_rolling(long, "team", column, settings.phase2_team_history_window)
    long["season_games_before"] = long.sort_values(["team", "season", "kickoff_utc"]).groupby(["team", "season"]).cumcount()
    long["feature_cutoff_utc"] = long["kickoff_utc"]
    base_features = [f"pregame_{c}" for c in roll_cols]
    home = long[long["is_home"].eq(1)][["game_id", "team", "opponent", "season_games_before", *base_features]].rename(columns={"team": "home_team", "opponent": "away_team", "season_games_before": "home_season_games_before", **{c: f"home_{c}" for c in base_features}})
    away = long[long["is_home"].eq(0)][["game_id", "team", "season_games_before", *base_features]].rename(columns={"team": "away_team", "season_games_before": "away_season_games_before", **{c: f"away_{c}" for c in base_features}})
    matrix = played.merge(home, on=["game_id", "home_team", "away_team"], how="left").merge(away, on=["game_id", "away_team"], how="left")
    matrix = matrix.merge(_elo_features(played), on="game_id", how="left")
    context_path = settings.root / "data" / "phase2_external" / "games.csv"
    if context_path.exists():
        context = pd.read_csv(context_path, usecols=["game_id", "home_rest", "away_rest", "location", "roof", "surface", "stadium"], low_memory=False)
        matrix = matrix.drop(columns=[c for c in ("location", "roof", "surface", "stadium") if c in matrix]).merge(context, on="game_id", how="left")
    matrix["home_rest_days"] = pd.to_numeric(matrix.get("home_rest", 7), errors="coerce").fillna(7)
    matrix["away_rest_days"] = pd.to_numeric(matrix.get("away_rest", 7), errors="coerce").fillna(7)
    matrix["elo_difference"] = matrix["home_elo"] - matrix["away_elo"]
    matrix["home_advantage"] = 1.0
    matrix["week_number"] = matrix["week"].astype(float)
    matrix["home_short_week"] = matrix["home_rest_days"].lt(7).astype(float)
    matrix["away_short_week"] = matrix["away_rest_days"].lt(7).astype(float)
    matrix["home_bye_rest"] = matrix["home_rest_days"].ge(10).astype(float)
    matrix["away_bye_rest"] = matrix["away_rest_days"].ge(10).astype(float)
    matrix["neutral_site"] = matrix.get("location", "Home").eq("Neutral").astype(float)
    matrix["international_game"] = matrix.get("stadium", "").fillna("").str.contains("Wembley|Tottenham|Twickenham|Azteca|Allianz|Deutsche|Bernab|Corinthians|Melbourne", case=False, regex=True).astype(float)
    matrix["roof_closed_or_dome"] = matrix.get("roof", "").fillna("").str.lower().isin(["closed", "dome"]).astype(float)
    matrix["surface_grass"] = matrix.get("surface", "").fillna("").str.lower().str.contains("grass").astype(float)
    # Weather contract exists, but historical schedule weather is an observed result field without a proven pregame timestamp.
    matrix["temperature"] = np.nan
    matrix["wind"] = np.nan
    matrix["precipitation"] = np.nan
    matrix["weather_reliability"] = "UNAVAILABLE_NOT_USED"
    # Pregame matchup terms combine each offense with the opponent's allowed efficiency state.
    for metric in ("epa_per_play", "pass_epa", "rush_epa", "success_rate", "explosive_rate", "turnover_rate"):
        h_off, a_off = f"home_pregame_{metric}", f"away_pregame_{metric}"
        h_def, a_def = f"home_pregame_def_{metric}", f"away_pregame_def_{metric}"
        if all(c in matrix for c in (h_off, a_off, h_def, a_def)):
            matrix[f"home_matchup_{metric}"] = matrix[h_off] - matrix[a_def]
            matrix[f"away_matchup_{metric}"] = matrix[a_off] - matrix[h_def]
    matrix["target_home_score"] = matrix["home_score"]
    matrix["target_away_score"] = matrix["away_score"]
    matrix["target_margin"] = matrix["margin"]
    matrix["target_total"] = matrix["game_total"]
    matrix, qb_features = augment_with_qb_features(settings, matrix)
    matrix["feature_version"] = settings.phase2_feature_version
    features = [c for c in matrix if c.startswith(("home_pregame_", "away_pregame_", "home_matchup_", "away_matchup_"))]
    features += ["home_elo", "away_elo", "elo_difference", "home_advantage", "week_number", "home_rest_days", "away_rest_days", "home_short_week", "away_short_week", "home_bye_rest", "away_bye_rest", "neutral_site", "international_game", "roof_closed_or_dome", "surface_grass", "home_season_games_before", "away_season_games_before", *qb_features]
    write_csv_json(pbp, settings.artifact_root / "phase2" / "team_game_efficiency.csv")
    # Identity/provenance is not a learned categorical feature, but live career
    # lookup and previous-starter classification require it in the archive.
    qb_metadata=[f'{side}_{field}' for side in ('home','away') for field in ('expected_qb_id','expected_qb_name','qb_source_reliability')]
    interface = [c for c in ("roof", "surface", "temperature", "wind", "precipitation", "weather_reliability",*qb_metadata) if c in matrix]
    write_csv_json(matrix[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team", *interface, *features, "target_home_score", "target_away_score", "target_margin", "target_total", "feature_version"]], settings.artifact_root / "phase2" / "team_advanced_matrix.csv")
    return matrix, features
