from __future__ import annotations

import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import PoissonRegressor, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from nfl2026.config import Settings
from nfl2026.utilities.io import write_csv_json


def _models(seed: int):
    return {
        "regularized_linear": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=30.0)),
        "poisson": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), PoissonRegressor(alpha=2.0, max_iter=1000)),
        "hist_gradient_boosting": make_pipeline(SimpleImputer(strategy="median"), HistGradientBoostingRegressor(max_iter=160, learning_rate=.035, max_leaf_nodes=12, l2_regularization=6.0, random_state=seed)),
        "gradient_boosting": make_pipeline(SimpleImputer(strategy="median"), GradientBoostingRegressor(n_estimators=140, learning_rate=.025, max_depth=2, min_samples_leaf=18, loss="huber", random_state=seed)),
        "extra_trees": make_pipeline(SimpleImputer(strategy="median"), ExtraTreesRegressor(n_estimators=180, max_depth=7, min_samples_leaf=10, max_features=.65, n_jobs=-1, random_state=seed)),
    }


def _metrics(frame: pd.DataFrame, model: str, fold: str) -> dict[str, object]:
    score_error = pd.concat([frame.pred_home_score - frame.actual_home_score, frame.pred_away_score - frame.actual_away_score], ignore_index=True)
    pred_margin = frame.pred_home_score - frame.pred_away_score
    actual_margin = frame.actual_home_score - frame.actual_away_score
    pred_total = frame.pred_home_score + frame.pred_away_score
    actual_total = frame.actual_home_score + frame.actual_away_score
    non_ties = actual_margin.ne(0)
    return {
        "track": "team_score", "model": model, "fold": fold, "games": len(frame),
        "team_score_mae": score_error.abs().mean(), "team_score_rmse": math.sqrt(float((score_error ** 2).mean())),
        "team_score_bias": score_error.mean(), "margin_mae": (pred_margin - actual_margin).abs().mean(),
        "total_mae": (pred_total - actual_total).abs().mean(),
        "winner_accuracy": ((pred_margin[non_ties] > 0) == (actual_margin[non_ties] > 0)).mean(),
        "calibration_brier": np.mean(((1 / (1 + np.exp(-pred_margin / 7.5))) - (actual_margin > 0).astype(float)) ** 2),
    }


def rolling_origin_team_models(settings: Settings, matrix: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    rows = []
    phase1_path = settings.artifact_root / "backtests" / "team_baseline_predictions.csv"
    phase1 = pd.read_csv(phase1_path) if phase1_path.exists() else pd.DataFrame()
    for target_season in (2023, 2024, 2025):
        train = matrix[matrix.season.lt(target_season)]
        test = matrix[matrix.season.eq(target_season)]
        if train.empty or test.empty:
            continue
        Xtr, Xte = train[features], test[features]
        fitted = {}
        for name, estimator in _models(settings.random_seed).items():
            home_model = estimator
            away_model = _models(settings.random_seed + 1)[name]
            home_model.fit(Xtr, train.target_home_score)
            away_model.fit(Xtr, train.target_away_score)
            fitted[name] = (home_model, away_model)
            hp = np.clip(home_model.predict(Xte), settings.score_min, settings.score_max)
            ap = np.clip(away_model.predict(Xte), settings.score_min, settings.score_max)
            for game, home, away in zip(test.itertuples(index=False), hp, ap):
                rows.append({"model": name, "fold": f"through_{target_season-1}_predict_{target_season}", "game_id": game.game_id, "season": target_season, "week": game.week, "kickoff_utc": game.kickoff_utc, "max_training_kickoff_utc": train.kickoff_utc.max(), "home_team": game.home_team, "away_team": game.away_team, "pred_home_score": home, "pred_away_score": away, "actual_home_score": game.target_home_score, "actual_away_score": game.target_away_score})
        # Transparent structural forecast: expected plays times smoothed points/play, home/away context retained.
        league_ppp = float(pd.concat([train.target_home_score, train.target_away_score]).mean() / 64.0)
        hplays = test.get("home_pregame_plays", pd.Series(64.0, index=test.index)).fillna(64).clip(45, 85)
        aplays = test.get("away_pregame_plays", pd.Series(64.0, index=test.index)).fillna(64).clip(45, 85)
        hepa = test.get("home_pregame_epa_per_play", pd.Series(0.0, index=test.index)).fillna(0)
        aepa = test.get("away_pregame_epa_per_play", pd.Series(0.0, index=test.index)).fillna(0)
        hp = np.clip(hplays * (league_ppp + .42 * hepa) + 1.2, 0, 60)
        ap = np.clip(aplays * (league_ppp + .42 * aepa), 0, 60)
        for game, home, away in zip(test.itertuples(index=False), hp, ap):
            rows.append({"model": "structural_possessions_efficiency", "fold": f"through_{target_season-1}_predict_{target_season}", "game_id": game.game_id, "season": target_season, "week": game.week, "kickoff_utc": game.kickoff_utc, "max_training_kickoff_utc": train.kickoff_utc.max(), "home_team": game.home_team, "away_team": game.away_team, "pred_home_score": home, "pred_away_score": away, "actual_home_score": game.target_home_score, "actual_away_score": game.target_away_score})
    predictions = pd.DataFrame(rows)
    if not phase1.empty:
        keep = phase1[phase1.model.isin(["dynamic_elo", "recent_points_for_against", "league_home_away"])].copy()
        predictions = pd.concat([predictions, keep[predictions.columns]], ignore_index=True)
    # Fixed ensemble weights are specified before scoring, never optimized on the target fold.
    piv_h = predictions.pivot_table(index=["game_id", "fold"], columns="model", values="pred_home_score")
    piv_a = predictions.pivot_table(index=["game_id", "fold"], columns="model", values="pred_away_score")
    source = predictions.drop_duplicates(["game_id", "fold"]).set_index(["game_id", "fold"])
    ensemble_rows = []
    required = ["regularized_linear", "hist_gradient_boosting", "dynamic_elo"]
    if all(c in piv_h for c in required):
        common = piv_h.dropna(subset=required).index.intersection(piv_a.dropna(subset=required).index)
        for idx in common:
            meta = source.loc[idx]
            wh = .45 * piv_h.loc[idx, "regularized_linear"] + .35 * piv_h.loc[idx, "hist_gradient_boosting"] + .20 * piv_h.loc[idx, "dynamic_elo"]
            wa = .45 * piv_a.loc[idx, "regularized_linear"] + .35 * piv_a.loc[idx, "hist_gradient_boosting"] + .20 * piv_a.loc[idx, "dynamic_elo"]
            ensemble_rows.append({**meta.to_dict(), "game_id": idx[0], "fold": idx[1], "model": "fixed_advanced_ensemble", "pred_home_score": wh, "pred_away_score": wa})
    predictions = pd.concat([predictions, pd.DataFrame(ensemble_rows)], ignore_index=True)
    metrics = []
    for (model, fold), group in predictions.groupby(["model", "fold"]):
        metrics.append(_metrics(group, model, fold))
    for model, group in predictions.groupby("model"):
        row = _metrics(group, model, "all_rolling_origin_folds")
        fold_mae = pd.DataFrame([_metrics(g, model, str(f)) for f, g in group.groupby("fold")]).team_score_mae
        row["fold_mae_std"] = fold_mae.std(ddof=0)
        metrics.append(row)
    comparison = pd.DataFrame(metrics).sort_values(["fold", "team_score_mae", "model"])
    overall = comparison[comparison.fold.eq("all_rolling_origin_folds")].copy()
    advanced = overall[~overall.model.isin(["dynamic_elo", "recent_points_for_against"])]
    best_advanced = advanced.sort_values(["team_score_mae", "calibration_brier", "fold_mae_std"]).iloc[0]
    baseline = overall[overall.model.eq("dynamic_elo")].iloc[0]
    selected = best_advanced if best_advanced.team_score_mae < baseline.team_score_mae else baseline
    selection = {"selected_model": selected.model, "retained_baseline": selected.model == "dynamic_elo", "reason": "lowest stable all-fold score MAE; advanced model must beat the locked Phase 1 hurdle", "baseline_mae": float(baseline.team_score_mae), "advanced_best_mae": float(best_advanced.team_score_mae)}
    write_csv_json(predictions, settings.artifact_root / "backtests" / "phase2_team_predictions.csv")
    write_csv_json(comparison, settings.report_root / "phase2_team_model_comparison.csv")
    return comparison, predictions, selection


def feature_ablation(settings: Settings, matrix: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    groups = {
        "full": features,
        "minus_qb": [c for c in features if "_qb_" not in c],
        "minus_opponent_adjustment": [c for c in features if "matchup_" not in c and "pregame_def_" not in c],
        "minus_pbp_efficiency": [c for c in features if not any(t in c for t in ("epa", "success", "explosive", "turnover", "sack", "red_zone"))],
        "minus_matchup_interactions": [c for c in features if "matchup_" not in c],
        "minus_prior_history": [c for c in features if "pregame_" not in c and "season_games_before" not in c],
        "minus_personnel": [c for c in features if "_qb_" not in c],
        "minus_dynamic_strength": [c for c in features if "elo" not in c],
        "schedule_and_dynamic_only": [c for c in features if any(t in c for t in ("elo", "week", "rest", "advantage", "games_before"))],
    }
    rows = []
    for name, subset in groups.items():
        fold_errors = []
        for season in (2023, 2024, 2025):
            train, test = matrix[matrix.season.lt(season)], matrix[matrix.season.eq(season)]
            model_h = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=30.0)).fit(train[subset], train.target_home_score)
            model_a = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=30.0)).fit(train[subset], train.target_away_score)
            errors = pd.concat([pd.Series(model_h.predict(test[subset]) - test.target_home_score.to_numpy()), pd.Series(model_a.predict(test[subset]) - test.target_away_score.to_numpy())])
            fold_errors.append((season, errors.abs().mean()))
        rows.append({"model": "regularized_linear", "ablation": name, "feature_count": len(subset), "team_score_mae": np.mean([x[1] for x in fold_errors]), "fold_mae_std": np.std([x[1] for x in fold_errors]), "seasons": "2023|2024|2025", "interpretation": "full point-in-time feature set" if name == "full" else f"removed/tested {name}"})
    result = pd.DataFrame(rows).sort_values("team_score_mae")
    write_csv_json(result, settings.report_root / "phase2_feature_ablation.csv")
    return result


def fit_production_team_model(settings: Settings, matrix: pd.DataFrame, features: list[str], selected_name: str) -> Path:
    name = selected_name if selected_name in _models(settings.random_seed) else "regularized_linear"
    home = _models(settings.random_seed)[name].fit(matrix[features], matrix.target_home_score)
    away = _models(settings.random_seed + 1)[name].fit(matrix[features], matrix.target_away_score)
    artifact = settings.artifact_root / "models" / f"team-{settings.phase2_model_version}.joblib"
    joblib.dump({"home": home, "away": away, "features": features, "selected_backtest_model": selected_name, "fitted_algorithm": name}, artifact)
    return artifact


def simulate_games(settings: Settings, point_predictions: pd.DataFrame, residuals: pd.DataFrame | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(settings.random_seed)
    if residuals is not None and len(residuals) > 20:
        hres = residuals.actual_home_score - residuals.pred_home_score
        ares = residuals.actual_away_score - residuals.pred_away_score
        covariance = np.cov(np.vstack([hres, ares]))
    else:
        covariance = np.array([[90.0, 12.0], [12.0, 90.0]])
    rows = []
    for row in point_predictions.itertuples(index=False):
        noise = rng.multivariate_normal([0, 0], covariance, size=settings.simulation_draws)
        home = np.clip(row.home_expected_points + noise[:, 0], 0, 70)
        away = np.clip(row.away_expected_points + noise[:, 1], 0, 70)
        margin, total = home - away, home + away
        rows.append({**row._asdict(), "home_win_probability": float(np.mean(margin > 0) + .5 * np.mean(margin == 0)), "away_win_probability": float(np.mean(margin < 0) + .5 * np.mean(margin == 0)), "margin_mean": margin.mean(), "total_mean": total.mean(), **{f"home_score_p{q}": np.quantile(home, q / 100) for q in (10, 25, 50, 75, 90)}, **{f"away_score_p{q}": np.quantile(away, q / 100) for q in (10, 25, 50, 75, 90)}, **{f"margin_p{q}": np.quantile(margin, q / 100) for q in (10, 25, 50, 75, 90)}, **{f"total_p{q}": np.quantile(total, q / 100) for q in (10, 25, 50, 75, 90)}, "simulation_draws": settings.simulation_draws})
    return pd.DataFrame(rows)
