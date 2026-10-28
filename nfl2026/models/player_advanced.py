from __future__ import annotations

import math

import joblib
import numpy as np
import pandas as pd
from scipy.special import expit, logit
from sklearn.base import clone
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, PoissonRegressor, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from nfl2026.config import Settings
from nfl2026.models.probability import expected_calibration_error, log_loss_binary
from nfl2026.utilities.io import write_csv_json


TARGETS = ["carries", "targets", "rushing_yards", "receiving_yards"]


def combine_opportunity_efficiency(opportunity: np.ndarray, efficiency: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(opportunity, dtype=float), 0, None) * np.clip(np.asarray(efficiency, dtype=float), 0, None)


def _regressors(seed: int):
    return {
        "regularized_linear": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=18.0)),
        "poisson": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), PoissonRegressor(alpha=1.5, max_iter=1000)),
        "hist_gradient_boosting": make_pipeline(SimpleImputer(strategy="median"), HistGradientBoostingRegressor(max_iter=120, learning_rate=.04, max_leaf_nodes=10, min_samples_leaf=24, l2_regularization=5, random_state=seed)),
        "extra_trees": make_pipeline(SimpleImputer(strategy="median"), ExtraTreesRegressor(n_estimators=140, max_depth=8, min_samples_leaf=10, max_features=.75, n_jobs=-1, random_state=seed)),
    }


def reconcile_td_probabilities(probabilities: np.ndarray, target_sum: float, tolerance: float = 1e-8) -> np.ndarray:
    p = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1 - 1e-6)
    target = float(np.clip(target_sum, 0, len(p)))
    if not len(p) or target <= 0:
        return np.zeros_like(p)
    if target >= len(p):
        return np.ones_like(p)
    lo, hi = -30.0, 30.0
    base = logit(p)
    for _ in range(100):
        mid = (lo + hi) / 2
        total = expit(base + mid).sum()
        if total < target:
            lo = mid
        else:
            hi = mid
        if abs(total - target) <= tolerance:
            break
    return expit(base + (lo + hi) / 2)


def _metric_row(frame: pd.DataFrame, model: str, track: str, fold: str) -> dict[str, object]:
    row = {"track": track, "model": model, "fold": fold, "player_games": len(frame), "evaluation_population": "actual participants supplied only as an independent scoring set" if track == "conditional_participant" else "weekly active-roster universe scored end to end"}
    for target in TARGETS:
        row[f"{target}_mae"] = (frame[f"pred_{target}"] - frame[f"actual_{target}"]).abs().mean()
        row[f"{target}_rmse"] = math.sqrt(float(((frame[f"pred_{target}"] - frame[f"actual_{target}"]) ** 2).mean()))
    return row


def rolling_origin_player_models(settings: Settings, conditional: pd.DataFrame, candidates: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, str]]:
    conditional = conditional.copy()
    prediction_rows = []
    candidate_rows = []
    for season in (2023, 2024, 2025):
        train = conditional[conditional.season.lt(season)]
        test = conditional[conditional.season.eq(season)]
        universe = candidates[candidates.season.eq(season)].copy()
        for name, base in _regressors(settings.random_seed).items():
            fold_predictions = test[["game_id", "player_id", "display_name", "team", "opponent", "position", "season", "week", "kickoff_utc"]].copy()
            universe_predictions = universe[["game_id", "player_id", "player_name", "team", "opponent", "position", "season", "week", "kickoff_utc"]].copy()
            for target in TARGETS:
                target_base = _regressors(settings.random_seed)["regularized_linear"] if name == "poisson" and target in {"rushing_yards", "receiving_yards"} else base
                model = clone(target_base).fit(train[features], train[f"target_{target}"])
                fold_predictions[f"pred_{target}"] = np.clip(model.predict(test[features]), 0, None)
                universe_predictions[f"pred_{target}"] = np.clip(model.predict(universe[features]), 0, None)
            fold_predictions["model"] = name
            universe_predictions["model"] = name
            prediction_rows.append(fold_predictions)
            candidate_rows.append(universe_predictions)
            if name in {"regularized_linear", "hist_gradient_boosting"}:
                staged = fold_predictions.copy()
                staged_u = universe_predictions.copy()
                ypc = test.recent_yards_per_carry.fillna(4.0).clip(1.5, 8.0)
                ypt = test.recent_yards_per_target.fillna(7.0).clip(2.0, 15.0)
                uypc = universe.recent_yards_per_carry.fillna(4.0).clip(1.5, 8.0)
                uypt = universe.recent_yards_per_target.fillna(7.0).clip(2.0, 15.0)
                staged["pred_rushing_yards"] = combine_opportunity_efficiency(staged.pred_carries, ypc)
                staged["pred_receiving_yards"] = combine_opportunity_efficiency(staged.pred_targets, ypt)
                staged_u["pred_rushing_yards"] = combine_opportunity_efficiency(staged_u.pred_carries, uypc)
                staged_u["pred_receiving_yards"] = combine_opportunity_efficiency(staged_u.pred_targets, uypt)
                staged["model"] = f"opportunity_first_{name}"
                staged_u["model"] = f"opportunity_first_{name}"
                prediction_rows.append(staged)
                candidate_rows.append(staged_u)
    predictions = pd.concat(prediction_rows, ignore_index=True)
    universe_predictions = pd.concat(candidate_rows, ignore_index=True)
    actual_cols = conditional[["game_id", "player_id", "position", "season", *[f"target_{t}" for t in TARGETS]]].rename(columns={f"target_{t}": f"actual_{t}" for t in TARGETS})
    conditional_scored = predictions.merge(actual_cols, on=["game_id", "player_id", "position", "season"], how="inner")
    metrics = []
    for (model, season), group in conditional_scored.groupby(["model", "season"]):
        metrics.append(_metric_row(group, model, "conditional_participant", f"predict_{season}"))
    for model, group in conditional_scored.groupby("model"):
        metrics.append(_metric_row(group, model, "conditional_participant", "all_rolling_origin_folds"))

    true_scored_parts = []
    actual_universe = conditional[["game_id", "player_id", "position", "season", "week", *[f"target_{t}" for t in TARGETS]]].rename(columns={f"target_{t}": f"actual_{t}" for t in TARGETS})
    for model, pred in universe_predictions.groupby("model"):
        scored = pred.merge(actual_universe, on=["game_id", "player_id", "position", "season", "week"], how="outer", indicator=True)
        scored["model"] = model
        for target in TARGETS:
            scored[f"pred_{target}"] = scored[f"pred_{target}"].fillna(0)
            scored[f"actual_{target}"] = scored[f"actual_{target}"].fillna(0)
        scored["actual_participant"] = scored["_merge"].ne("left_only")
        scored["predicted_eligible"] = scored["_merge"].ne("right_only")
        true_scored_parts.append(scored)
        metrics.append(_metric_row(scored, model, "true_pregame_universe", "all_rolling_origin_folds"))
    true_scored = pd.concat(true_scored_parts, ignore_index=True)
    comparison = pd.DataFrame(metrics)
    phase1_report = settings.report_root / "player_baseline_backtest.csv"
    if phase1_report.exists():
        phase1 = pd.read_csv(phase1_report)
        base = phase1[(phase1.season.astype(str).eq("all")) & phase1.position.eq("ALL")]
        if len(base):
            b = base.iloc[0]
            comparison = pd.concat([comparison, pd.DataFrame([{"track": "conditional_participant", "model": "phase1_recent_usage_baseline", "fold": "all_rolling_origin_folds", "player_games": int(b.player_games), "evaluation_population": b.evaluation_population, "carries_mae": b.carries_mae, "targets_mae": b.targets_mae, "rushing_yards_mae": b.rushing_yards_mae, "receiving_yards_mae": b.receiving_yards_mae}])], ignore_index=True)
    hurdle = {"carries": 1.325, "targets": 1.531, "rushing_yards": 7.873, "receiving_yards": 15.915}
    selections = {}
    overall = comparison[(comparison.track.eq("conditional_participant")) & comparison.fold.eq("all_rolling_origin_folds")]
    for target in TARGETS:
        best = overall.sort_values(f"{target}_mae").iloc[0]
        selections[target] = str(best.model) if best[f"{target}_mae"] < hurdle[target] else "phase1_recent_usage_baseline"
    write_csv_json(predictions, settings.artifact_root / "backtests" / "phase2_player_conditional_predictions.csv")
    write_csv_json(true_scored, settings.artifact_root / "backtests" / "phase2_player_universe_scored.csv")
    write_csv_json(comparison, settings.report_root / "phase2_player_model_comparison.csv")
    return comparison, conditional_scored, true_scored, selections


def td_model_backtest(settings: Settings, conditional: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, scored_parts = [], []
    use_features = features + ["recent_red_zone_carries", "recent_red_zone_targets"]
    use_features = list(dict.fromkeys(use_features))
    for season in (2023, 2024, 2025):
        train, test = conditional[conditional.season.lt(season)], conditional[conditional.season.eq(season)].copy()
        model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=.35, max_iter=1000, class_weight=None))
        model.fit(train[use_features], train.target_anytime_offensive_td.clip(0, 1))
        test["raw_probability"] = np.clip(model.predict_proba(test[use_features])[:, 1], .001, .999)
        test["team_td_environment"] = test.groupby(["game_id", "team"])["prior_anytime_offensive_td"].transform("sum").clip(.25, 5.5)
        reconciled = []
        for _, group in test.groupby(["game_id", "team"], sort=False):
            reconciled.extend(zip(group.index, reconcile_td_probabilities(group.raw_probability.to_numpy(), float(group.team_td_environment.iloc[0]))))
        mapped = dict(reconciled)
        test["reconciled_probability"] = test.index.map(mapped)
        test["actual_td"] = test.target_anytime_offensive_td.clip(0, 1)
        scored_parts.append(test[["game_id", "player_id", "display_name", "team", "position", "season", "week", "raw_probability", "reconciled_probability", "team_td_environment", "actual_td"]])
        for version, column in (("raw", "raw_probability"), ("reconciled", "reconciled_probability")):
            y, p = test.actual_td.to_numpy(), test[column].to_numpy()
            rows.append({"model": "regularized_logistic_td", "probability_version": version, "fold": f"predict_{season}", "player_games": len(test), "brier_score": np.mean((p-y)**2), "log_loss": log_loss_binary(y,p), "expected_calibration_error": expected_calibration_error(y,p), "mean_probability": p.mean(), "event_rate": y.mean(), "mean_team_probability_sum": test.groupby(["game_id","team"])[column].sum().mean()})
    scored = pd.concat(scored_parts, ignore_index=True)
    for version, column in (("raw", "raw_probability"), ("reconciled", "reconciled_probability")):
        y, p = scored.actual_td.to_numpy(), scored[column].to_numpy()
        rows.append({"model": "regularized_logistic_td", "probability_version": version, "fold": "all_rolling_origin_folds", "player_games": len(scored), "brier_score": np.mean((p-y)**2), "log_loss": log_loss_binary(y,p), "expected_calibration_error": expected_calibration_error(y,p), "mean_probability": p.mean(), "event_rate": y.mean(), "mean_team_probability_sum": scored.groupby(["game_id","team"])[column].sum().mean()})
    calibration = pd.DataFrame(rows)
    write_csv_json(calibration, settings.report_root / "phase2_td_calibration.csv")
    write_csv_json(scored, settings.artifact_root / "backtests" / "phase2_td_predictions.csv")
    return calibration, scored


def position_metrics(settings: Settings, scored: pd.DataFrame, td_scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    best_model = scored.groupby("model").apply(lambda g: np.mean([(g[f"pred_{t}"]-g[f"actual_{t}"]).abs().mean() for t in TARGETS]), include_groups=False).sort_values().index[0]
    subset = scored[scored.model.eq(best_model)]
    for position, group in subset.groupby("position"):
        row = _metric_row(group, str(best_model), "conditional_participant", "all_rolling_origin_folds")
        row["position"] = position
        td = td_scored[td_scored.position.eq(position)]
        if len(td):
            row["td_brier_raw"] = np.mean((td.raw_probability-td.actual_td)**2)
            row["td_brier_reconciled"] = np.mean((td.reconciled_probability-td.actual_td)**2)
        rows.append(row)
    result = pd.DataFrame(rows)
    write_csv_json(result, settings.report_root / "phase2_player_position_metrics.csv")
    return result


def fit_production_player_models(settings: Settings, conditional: pd.DataFrame, features: list[str], selections: dict[str, str]) -> str:
    artifacts = {}
    for target in TARGETS:
        selected = selections[target]
        base_name = selected.replace("opportunity_first_", "")
        if base_name == "phase1_recent_usage_baseline" or base_name not in _regressors(settings.random_seed):
            artifacts[target] = {"fallback": f"prior_{target}"}
        else:
            artifacts[target] = clone(_regressors(settings.random_seed)[base_name]).fit(conditional[features], conditional[f"target_{target}"])
    path = settings.artifact_root / "models" / f"player-{settings.phase2_model_version}.joblib"
    joblib.dump({"models": artifacts, "features": features, "selections": selections}, path)
    return str(path)
