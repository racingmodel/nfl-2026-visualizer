from __future__ import annotations

from collections import defaultdict, deque
import math

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.features.core import team_long_table
from nfl2026.models.probability import clip_probability, expected_calibration_error, log_loss_binary
from nfl2026.utilities.io import write_csv_json


def assert_chronological_separation(frame: pd.DataFrame) -> None:
    cutoff = pd.to_datetime(frame["max_training_kickoff_utc"], utc=True)
    target = pd.to_datetime(frame["kickoff_utc"], utc=True)
    invalid = cutoff.notna() & (cutoff >= target)
    if invalid.any():
        raise AssertionError(f"{int(invalid.sum())} predictions overlap their training window")


def _team_metrics(frame: pd.DataFrame, model: str, fold: str) -> dict[str, object]:
    home_error = frame["pred_home_score"] - frame["actual_home_score"]
    away_error = frame["pred_away_score"] - frame["actual_away_score"]
    score_error = pd.concat([home_error, away_error], ignore_index=True)
    pred_margin = frame["pred_home_score"] - frame["pred_away_score"]
    actual_margin = frame["actual_home_score"] - frame["actual_away_score"]
    pred_total = frame["pred_home_score"] + frame["pred_away_score"]
    actual_total = frame["actual_home_score"] + frame["actual_away_score"]
    non_ties = actual_margin.ne(0)
    return {
        "model": model,
        "fold": fold,
        "games": len(frame),
        "team_score_mae": score_error.abs().mean(),
        "team_score_rmse": math.sqrt(float((score_error**2).mean())),
        "team_score_bias": score_error.mean(),
        "margin_mae": (pred_margin - actual_margin).abs().mean(),
        "total_mae": (pred_total - actual_total).abs().mean(),
        "winner_accuracy": ((pred_margin[non_ties] > 0) == (actual_margin[non_ties] > 0)).mean(),
    }


def team_baseline_backtest(settings: Settings, games: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    played = games[games["result_status"].eq("completed") & games["game_type"].eq("REG")].sort_values(["kickoff_utc", "game_id"])
    recent_for: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=settings.team_rolling_window))
    recent_against: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=settings.team_rolling_window))
    all_home: list[float] = []
    all_away: list[float] = []
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    last_kickoff: pd.Timestamp | None = None
    rows: list[dict[str, object]] = []
    # Treat equal-kickoff games as one prediction block. No result in a Sunday window can train another game in that same window.
    for kickoff_value, block in played.groupby("kickoff_utc", sort=True):
        kickoff = pd.Timestamp(kickoff_value)
        for game in block.itertuples(index=False):
            if int(game.season) >= 2023 and all_home:
                league_home = float(np.mean(all_home))
                league_away = float(np.mean(all_away))
                h_for = float(np.mean(recent_for[game.home_team])) if recent_for[game.home_team] else league_home
                h_against = float(np.mean(recent_against[game.home_team])) if recent_against[game.home_team] else league_away
                a_for = float(np.mean(recent_for[game.away_team])) if recent_for[game.away_team] else league_away
                a_against = float(np.mean(recent_against[game.away_team])) if recent_against[game.away_team] else league_home
                forecasts = {
                    "league_home_away": (league_home, league_away),
                    "recent_points_for_against": ((h_for + a_against) / 2, (a_for + h_against) / 2),
                }
                expected_home = 1 / (1 + 10 ** (-(elo[game.home_team] + 45 - elo[game.away_team]) / 400))
                margin = (expected_home - 0.5) * 28
                total = float(np.mean(all_home) + np.mean(all_away))
                forecasts["dynamic_elo"] = (total / 2 + margin / 2, total / 2 - margin / 2)
                for model, (pred_home, pred_away) in forecasts.items():
                    rows.append({
                        "model": model,
                        "fold": f"through_{int(game.season) - 1}_predict_{int(game.season)}",
                        "game_id": game.game_id,
                        "season": int(game.season),
                        "week": int(game.week),
                        "kickoff_utc": kickoff,
                        "max_training_kickoff_utc": last_kickoff,
                        "home_team": game.home_team,
                        "away_team": game.away_team,
                        "pred_home_score": max(0.0, pred_home),
                        "pred_away_score": max(0.0, pred_away),
                        "actual_home_score": game.home_score,
                        "actual_away_score": game.away_score,
                    })
        for game in block.itertuples(index=False):
            home_result = 1.0 if game.home_score > game.away_score else (0.5 if game.home_score == game.away_score else 0.0)
            expectation = 1 / (1 + 10 ** (-(elo[game.home_team] + 45 - elo[game.away_team]) / 400))
            margin_multiplier = math.log(abs(game.home_score - game.away_score) + 1) * (2.2 / ((elo[game.home_team] - elo[game.away_team]) * 0.001 + 2.2))
            change = 20 * margin_multiplier * (home_result - expectation)
            elo[game.home_team] += change
            elo[game.away_team] -= change
            all_home.append(float(game.home_score))
            all_away.append(float(game.away_score))
            recent_for[game.home_team].append(float(game.home_score))
            recent_against[game.home_team].append(float(game.away_score))
            recent_for[game.away_team].append(float(game.away_score))
            recent_against[game.away_team].append(float(game.home_score))
        last_kickoff = kickoff

    predictions = pd.DataFrame(rows)
    assert_chronological_separation(predictions)
    metrics: list[dict[str, object]] = []
    for (model, fold), group in predictions.groupby(["model", "fold"]):
        metrics.append(_team_metrics(group, model, fold))
    for model, group in predictions.groupby("model"):
        metrics.append(_team_metrics(group, model, "all_rolling_origin_folds"))
    summary = pd.DataFrame(metrics).sort_values(["fold", "team_score_mae", "model"]).reset_index(drop=True)
    write_csv_json(predictions, settings.artifact_root / "backtests" / "team_baseline_predictions.csv")
    write_csv_json(summary, settings.artifact_root / "reports" / "team_baseline_backtest.csv")
    write_csv_json(summary, settings.report_root / "team_baseline_backtest.csv")
    return summary, predictions


def player_baseline_backtest(settings: Settings, matrix: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    predictions = matrix[matrix["season"].ge(2023)].copy()
    for target in ("carries", "targets", "rushing_yards", "receiving_yards"):
        predictions[f"pred_{target}"] = predictions[f"prior_{target}"].fillna(0).clip(lower=0)
    predictions["pred_td_probability"] = clip_probability(predictions["prior_anytime_offensive_td"].fillna(0.02))
    predictions["actual_td"] = predictions["target_anytime_offensive_td"].clip(0, 1)
    predictions["evaluation_population"] = "actual_week_participants_conditional"
    predictions["max_training_kickoff_utc"] = predictions["prediction_cutoff_utc"].map(
        lambda value: pd.Timestamp(value) - pd.Timedelta(nanoseconds=1)
    )
    predictions["kickoff_utc"] = predictions["prediction_cutoff_utc"]
    assert_chronological_separation(predictions)

    def metrics(group: pd.DataFrame, season: str, position: str) -> dict[str, object]:
        actual = group["actual_td"].to_numpy()
        probability = group["pred_td_probability"].to_numpy()
        return {
            "model": "recent_usage_with_position_fallback",
            "season": season,
            "position": position,
            "player_games": len(group),
            "carries_mae": (group["pred_carries"] - group["target_carries"]).abs().mean(),
            "targets_mae": (group["pred_targets"] - group["target_targets"]).abs().mean(),
            "rushing_yards_mae": (group["pred_rushing_yards"] - group["target_rushing_yards"]).abs().mean(),
            "receiving_yards_mae": (group["pred_receiving_yards"] - group["target_receiving_yards"]).abs().mean(),
            "td_brier_score": float(np.mean((probability - actual) ** 2)),
            "td_log_loss": log_loss_binary(actual, probability),
            "td_expected_calibration_error": expected_calibration_error(actual, probability),
            "evaluation_population": "actual_week_participants_conditional",
        }

    rows: list[dict[str, object]] = [metrics(predictions, "all", "ALL")]
    for season, group in predictions.groupby("season"):
        rows.append(metrics(group, str(int(season)), "ALL"))
    for position, group in predictions[predictions["position"].isin(["QB", "RB", "WR", "TE"])].groupby("position"):
        rows.append(metrics(group, "all", str(position)))
    summary = pd.DataFrame(rows)
    output_columns = [
        "game_id", "player_id", "display_name", "team", "opponent", "position", "season", "week", "kickoff_utc",
        "max_training_kickoff_utc", "pred_carries", "pred_targets", "pred_rushing_yards", "pred_receiving_yards",
        "pred_td_probability", "target_carries", "target_targets", "target_rushing_yards", "target_receiving_yards", "actual_td",
        "evaluation_population",
    ]
    write_csv_json(predictions[output_columns], settings.artifact_root / "backtests" / "player_baseline_predictions.csv")
    write_csv_json(summary, settings.artifact_root / "reports" / "player_baseline_backtest.csv")
    write_csv_json(summary, settings.report_root / "player_baseline_backtest.csv")
    return summary, predictions[output_columns]


def season_transition_backtest(settings: Settings, games: pd.DataFrame) -> pd.DataFrame:
    long = team_long_table(games)
    rows: list[dict[str, object]] = []
    for target_season in (2023, 2024, 2025):
        previous = long[long["season"].eq(target_season - 1)].groupby("team")[["points_for", "points_against"]].mean()
        target = long[(long["season"].eq(target_season)) & (long["week"].between(1, 4))]
        for pseudo_games in (1.0, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0):
            predictions: list[dict[str, object]] = []
            for game_id, pair in target.groupby("game_id"):
                if len(pair) != 2:
                    continue
                estimates: dict[str, float] = {}
                for row in pair.itertuples(index=False):
                    earlier = long[
                        (long["season"].eq(target_season))
                        & (long["team"].eq(row.team))
                        & (pd.to_datetime(long["kickoff_utc"], utc=True) < pd.Timestamp(row.kickoff_utc))
                    ]
                    prior_for = previous.loc[row.team, "points_for"] if row.team in previous.index else long[long["season"].lt(target_season)]["points_for"].mean()
                    prior_against = previous.loc[row.team, "points_against"] if row.team in previous.index else long[long["season"].lt(target_season)]["points_against"].mean()
                    current_count = len(earlier)
                    current_for = earlier["points_for"].mean() if current_count else prior_for
                    current_against = earlier["points_against"].mean() if current_count else prior_against
                    estimates[f"{row.team}_for"] = (pseudo_games * prior_for + current_count * current_for) / (pseudo_games + current_count)
                    estimates[f"{row.team}_against"] = (pseudo_games * prior_against + current_count * current_against) / (pseudo_games + current_count)
                home = pair[pair["is_home"].eq(1)].iloc[0]
                away = pair[pair["is_home"].eq(0)].iloc[0]
                predictions.append({
                    "pred_home": (estimates[f"{home.team}_for"] + estimates[f"{away.team}_against"]) / 2,
                    "pred_away": (estimates[f"{away.team}_for"] + estimates[f"{home.team}_against"]) / 2,
                    "actual_home": home.points_for,
                    "actual_away": away.points_for,
                })
            pred = pd.DataFrame(predictions)
            score_errors = pd.concat([pred["pred_home"] - pred["actual_home"], pred["pred_away"] - pred["actual_away"]], ignore_index=True)
            rows.append({
                "transition": f"{target_season - 1}_to_{target_season}",
                "target_season": target_season,
                "weeks": "1-4",
                "prior_pseudo_games": pseudo_games,
                "games": len(pred),
                "team_score_mae": score_errors.abs().mean(),
                "team_score_rmse": math.sqrt(float((score_errors**2).mean())),
            })
    report = pd.DataFrame(rows)
    aggregate = report.groupby("prior_pseudo_games", as_index=False).agg(team_score_mae=("team_score_mae", "mean"))
    best = float(aggregate.sort_values("team_score_mae").iloc[0]["prior_pseudo_games"])
    report["selected_global_weight"] = report["prior_pseudo_games"].eq(best)
    report["current_evidence_weight_after_n_games"] = report["prior_pseudo_games"].map(lambda p: f"n/({p:g}+n)")
    write_csv_json(report, settings.artifact_root / "reports" / "season_transition_backtest.csv")
    write_csv_json(report, settings.report_root / "season_transition_backtest.csv")
    return report
