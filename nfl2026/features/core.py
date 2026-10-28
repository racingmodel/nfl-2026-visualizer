from __future__ import annotations

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.utilities.io import write_csv_json


TEAM_STATS = ["points_for", "points_against"]
PLAYER_TARGETS = ["carries", "targets", "rushing_yards", "receiving_yards", "anytime_offensive_td"]


def shifted_rolling(frame: pd.DataFrame, group: str, value: str, window: int) -> pd.Series:
    ordered = frame.sort_values([group, "kickoff_utc", "game_id"])
    values = ordered.groupby(group, sort=False)[value].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
    return values.reindex(frame.index)


def team_long_table(games: pd.DataFrame) -> pd.DataFrame:
    completed = games[games["result_status"].eq("completed") & games["game_type"].eq("REG")].copy()
    home = completed[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team", "home_score", "away_score"]].rename(
        columns={"home_team": "team", "away_team": "opponent", "home_score": "points_for", "away_score": "points_against"}
    )
    home["is_home"] = 1
    away = completed[["game_id", "season", "week", "kickoff_utc", "away_team", "home_team", "away_score", "home_score"]].rename(
        columns={"away_team": "team", "home_team": "opponent", "away_score": "points_for", "home_score": "points_against"}
    )
    away["is_home"] = 0
    return pd.concat([home, away], ignore_index=True).sort_values(["kickoff_utc", "game_id", "is_home"]).reset_index(drop=True)


def build_team_features(settings: Settings, games: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    long = team_long_table(games)
    for stat in TEAM_STATS:
        long[f"rolling_{stat}_{settings.team_rolling_window}"] = shifted_rolling(long, "team", stat, settings.team_rolling_window)
        long[f"history_{stat}_mean"] = (
            long.sort_values(["team", "kickoff_utc", "game_id"])
            .groupby("team")[stat]
            .transform(lambda s: s.shift(1).expanding().mean())
            .reindex(long.index)
        )
    long["games_before"] = long.groupby("team").cumcount()
    long["feature_cutoff_utc"] = long["kickoff_utc"]
    long["feature_version"] = settings.feature_version

    feature_columns = [
        f"rolling_points_for_{settings.team_rolling_window}",
        f"rolling_points_against_{settings.team_rolling_window}",
        "history_points_for_mean", "history_points_against_mean", "games_before",
    ]
    home = long[long["is_home"].eq(1)][["game_id", "team", "opponent", *feature_columns]].rename(
        columns={"team": "home_team", "opponent": "away_team", **{c: f"home_{c}" for c in feature_columns}}
    )
    away = long[long["is_home"].eq(0)][["game_id", "team", *feature_columns]].rename(
        columns={"team": "away_team", **{c: f"away_{c}" for c in feature_columns}}
    )
    matrix = games[games["result_status"].eq("completed") & games["game_type"].eq("REG")][
        ["game_id", "season", "week", "kickoff_utc", "home_team", "away_team", "home_score", "away_score", "margin", "game_total"]
    ].merge(home, on=["game_id", "home_team", "away_team"], how="left").merge(away, on=["game_id", "away_team"], how="left")
    matrix = matrix.rename(columns={"kickoff_utc": "prediction_cutoff_utc"})
    matrix["feature_version"] = settings.feature_version
    matrix["target_home_score"] = matrix.pop("home_score")
    matrix["target_away_score"] = matrix.pop("away_score")
    matrix["target_margin"] = matrix.pop("margin")
    matrix["target_total"] = matrix.pop("game_total")
    write_csv_json(long, settings.artifact_root / "features" / "team_features.csv")
    write_csv_json(matrix, settings.artifact_root / "features" / "team_training_matrix.csv")
    return long, matrix


def _position_priors(frame: pd.DataFrame, target: str) -> pd.Series:
    # Aggregate at each kickoff before expanding, so players in the same game window cannot leak into one another.
    moments = frame.groupby(["position", "kickoff_utc"], dropna=False)[target].agg(["sum", "count"]).reset_index()
    moments = moments.sort_values(["position", "kickoff_utc"])
    moments["prior_sum"] = moments.groupby("position")["sum"].cumsum() - moments["sum"]
    moments["prior_count"] = moments.groupby("position")["count"].cumsum() - moments["count"]
    moments[f"position_prior_{target}"] = moments["prior_sum"] / moments["prior_count"].replace(0, np.nan)
    keyed = frame[["position", "kickoff_utc"]].merge(
        moments[["position", "kickoff_utc", f"position_prior_{target}"]],
        on=["position", "kickoff_utc"], how="left",
    )
    return keyed[f"position_prior_{target}"].set_axis(frame.index)


def build_player_features(settings: Settings, weekly: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    fixture = pd.concat(
        [
            games[["game_id", "season", "week", "home_team", "away_team", "kickoff_utc"]].rename(columns={"home_team": "team", "away_team": "scheduled_opponent"}),
            games[["game_id", "season", "week", "away_team", "home_team", "kickoff_utc"]].rename(columns={"away_team": "team", "home_team": "scheduled_opponent"}),
        ], ignore_index=True,
    )
    rows = weekly.merge(fixture, on=["season", "week", "team"], how="inner")
    rows = rows.sort_values(["kickoff_utc", "game_id", "player_id"]).reset_index(drop=True)
    rows["position"] = rows["position"].fillna("UNK")
    rows["player_games_before"] = rows.groupby("player_id").cumcount()
    for target in PLAYER_TARGETS:
        rows[f"recent_{target}"] = shifted_rolling(rows, "player_id", target, settings.player_history_window)
        rows[f"position_prior_{target}"] = _position_priors(rows, target)
        rows[f"prior_{target}"] = rows[f"recent_{target}"].fillna(rows[f"position_prior_{target}"])
    rows["rookie_or_no_history"] = rows["player_games_before"].eq(0).astype(int)
    rows["feature_cutoff_utc"] = rows["kickoff_utc"]
    rows["feature_version"] = settings.feature_version
    matrix_columns = [
        "game_id", "player_id", "display_name", "team", "scheduled_opponent", "position", "season", "week",
        "feature_cutoff_utc", "feature_version", "player_games_before", "rookie_or_no_history",
        *[f"recent_{t}" for t in PLAYER_TARGETS], *[f"position_prior_{t}" for t in PLAYER_TARGETS], *[f"prior_{t}" for t in PLAYER_TARGETS],
        *PLAYER_TARGETS,
    ]
    matrix = rows[matrix_columns].rename(columns={
        "scheduled_opponent": "opponent", "feature_cutoff_utc": "prediction_cutoff_utc",
        **{target: f"target_{target}" for target in PLAYER_TARGETS},
    })
    write_csv_json(rows, settings.artifact_root / "features" / "player_features.csv")
    write_csv_json(matrix, settings.artifact_root / "features" / "player_training_matrix.csv")
    return matrix


def assert_no_forbidden_features(columns: list[str], forbidden_tokens: tuple[str, ...]) -> None:
    bad = [column for column in columns if any(token.lower() in column.lower() for token in forbidden_tokens)]
    if bad:
        raise ValueError(f"Forbidden market/bookmaker features: {bad}")


def feature_quality_report(
    settings: Settings,
    datasets: dict[str, tuple[pd.DataFrame, list[str]]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for dataset_name, (frame, features) in datasets.items():
        assert_no_forbidden_features(features, settings.forbidden_feature_tokens)
        for feature in features:
            values = pd.to_numeric(frame[feature], errors="coerce")
            non_null = values.notna().mean()
            zero_pct = values.eq(0).mean()
            unique = values.nunique(dropna=True)
            variance = values.var()
            dominant_pct = values.value_counts(normalize=True, dropna=True).max() if values.notna().any() else np.nan
            flags: list[str] = []
            if unique <= 1:
                flags.append("constant")
            elif pd.notna(dominant_pct) and dominant_pct >= settings.near_constant_threshold:
                flags.append("near_constant")
            if non_null < 1 - settings.mostly_missing_threshold:
                flags.append("mostly_missing")
            rows.append({
                "dataset": dataset_name,
                "feature": feature,
                "row_count": len(frame),
                "non_null_percentage": non_null,
                "zero_percentage": zero_pct,
                "dominant_value_percentage": dominant_pct,
                "unique_values": unique,
                "variance": variance,
                "minimum": values.min(),
                "maximum": values.max(),
                "flags": "|".join(flags) if flags else "usable",
                "included_in_baseline": not flags,
            })
    report = pd.DataFrame(rows)
    write_csv_json(report, settings.artifact_root / "reports" / "feature_quality_report.csv")
    write_csv_json(report, settings.report_root / "feature_quality_report.csv")
    return report
