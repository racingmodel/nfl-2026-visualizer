"""Experimental possession-level NFL score simulator; not a production model.

Only pregame features and previous-season drive/event outcomes enter a fit.
The existing continuous team-score forecast is an input, never refitted here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from nfl2026.models.score_distribution import feasible_totals, joint_distribution, score_summary


@dataclass(frozen=True)
class PossessionModel:
    training_seasons: tuple[int, ...]
    training_games: int
    league_drives: float
    drive_intercept: np.ndarray
    drive_coef: np.ndarray
    drive_residual_pairs: np.ndarray
    rare_event_pairs: np.ndarray
    td_rate: float
    fg_rate: float
    red_zone_mean: np.ndarray
    red_zone_scale: np.ndarray
    scoring_share_coef: np.ndarray
    scoring_share_intercept: float
    conversion_probabilities: np.ndarray  # six, seven, eight points per TD
    feasible_scores: np.ndarray


def _game_pairs(history: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    good = history[history.research_valid.astype(bool)].copy()
    values = ["offensive_possessions", "defensive_td", "special_teams_td",
              "safeties", "defensive_two_point_made"]
    side = good.pivot(index="game_id", columns="side", values=values)
    side.columns = [f"{v}_{s.lower()}" for v, s in side.columns]
    side = side.reset_index()
    columns = ["game_id", "home_pregame_drives", "away_pregame_drives"]
    return side.merge(features[columns].drop_duplicates("game_id"), on="game_id", how="inner", validate="one_to_one").dropna(subset=["offensive_possessions_home", "offensive_possessions_away"])


def fit_possession_model(history: pd.DataFrame, features: pd.DataFrame) -> PossessionModel:
    """Fit drive counts/outcomes on reconciled prior games only."""
    pairs = _game_pairs(history, features)
    if len(pairs) < 100:
        raise ValueError("At least 100 reconciled historical games are required")
    valid = history[history.game_id.isin(pairs.game_id) & history.research_valid.astype(bool)]
    league_drives = float(valid.offensive_possessions.mean())
    x = pairs[["home_pregame_drives", "away_pregame_drives"]].astype(float).fillna(league_drives).to_numpy()
    x = np.column_stack([np.ones(len(x)), x - league_drives])
    y = pairs[["offensive_possessions_home", "offensive_possessions_away"]].astype(float).to_numpy()
    # Modest ridge shrinkage: early-season pregame drive priors are noisy.
    penalty = np.diag([0.0, 30.0, 30.0])
    coef = np.linalg.solve(x.T @ x + penalty, x.T @ y)
    predicted = x @ coef
    residuals = y - predicted
    rare = pairs[[f"{component}_{side}" for side in ("home", "away")
                  for component in ("defensive_td", "special_teams_td", "safeties", "defensive_two_point_made")]].fillna(0).astype(int).to_numpy()
    td = int(valid.offensive_td.sum())
    other_td = int(valid.defensive_td.sum() + valid.special_teams_td.sum())
    total_td = td + other_td
    pat = int(valid.pat_made.sum())
    two = int(valid.two_point_made.sum())
    if total_td < 1 or pat + two > total_td:
        raise ValueError("Historical TD conversion counts do not reconcile")
    conversion = np.array([total_td-pat-two, pat, two], dtype=float) / total_td
    possessions = float(valid.offensive_possessions.sum())
    # Alternative prior: pregame red-zone tendencies alter TD-vs-FG share.
    red_columns = ["game_id", "home_pregame_red_zone_td_rate",
                   "away_pregame_red_zone_td_rate", "home_pregame_def_red_zone_td_rate",
                   "away_pregame_def_red_zone_td_rate"]
    context = valid.merge(features[red_columns].drop_duplicates("game_id"), on="game_id",
                          how="left", validate="many_to_one")
    home = context.side.eq("HOME").to_numpy()
    offense = np.where(home, context.home_pregame_red_zone_td_rate, context.away_pregame_red_zone_td_rate).astype(float)
    defense = np.where(home, context.away_pregame_def_red_zone_td_rate, context.home_pregame_def_red_zone_td_rate).astype(float)
    red_mean = np.array([np.nanmean(offense), np.nanmean(defense)])
    red_scale = np.maximum([np.nanstd(offense),np.nanstd(defense)],.01)
    predictors = np.column_stack([np.ones(len(context)),
                                  (np.nan_to_num(offense, nan=red_mean[0])-red_mean[0])/red_scale[0],
                                  (np.nan_to_num(defense, nan=red_mean[1])-red_mean[1])/red_scale[1]])
    scored = (context.td_possessions+context.fg_possessions).to_numpy(dtype=float)
    share = np.divide(context.td_possessions.to_numpy(dtype=float),scored,out=np.zeros(len(scored)),where=scored>0)
    weights = scored.clip(min=0)
    share_coef = np.linalg.solve(predictors.T@(weights[:,None]*predictors)+np.diag([0,20,20]),
                                 predictors.T@(weights*share))
    return PossessionModel(
        training_seasons=tuple(sorted(valid.season.astype(int).unique())),
        training_games=len(pairs), league_drives=league_drives,
        drive_intercept=coef[0], drive_coef=coef[1:],
        drive_residual_pairs=residuals, rare_event_pairs=rare,
        td_rate=float(valid.td_possessions.sum()/possessions),
        fg_rate=float(valid.fg_possessions.sum()/possessions),
        red_zone_mean=red_mean, red_zone_scale=red_scale, scoring_share_coef=share_coef[1:],
        scoring_share_intercept=float(share_coef[0]),
        conversion_probabilities=conversion,
        feasible_scores=feasible_totals(valid),
    )


def expected_drives(model: PossessionModel, home_pregame_drives: float, away_pregame_drives: float) -> np.ndarray:
    inputs = np.array([home_pregame_drives, away_pregame_drives], dtype=float)
    inputs = np.where(np.isfinite(inputs), inputs, model.league_drives)
    return np.clip(model.drive_intercept + (inputs-model.league_drives) @ model.drive_coef, 1.0, 24.0)


def anchored_outcome_probabilities(model: PossessionModel, expected_points: float, drives: float, rare_mean: float,
                                   prior_td: float | None = None, prior_fg: float | None = None) -> np.ndarray:
    """Exponential tilt of 0/FG/TD possession odds to the team-score mean."""
    td_points = float(np.dot([6, 7, 8], model.conversion_probabilities))
    target = float(np.clip((expected_points-rare_mean)/drives, 0.001, td_points-0.001))
    values = np.array([0.0, 3.0, td_points])
    prior_td = model.td_rate if prior_td is None else prior_td
    prior_fg = model.fg_rate if prior_fg is None else prior_fg
    prior = np.array([1-prior_td-prior_fg, prior_fg, prior_td])
    if np.any(prior <= 0):
        raise ValueError("Invalid historical possession outcome rates")
    def probabilities(theta: float) -> np.ndarray:
        logits = np.log(prior) + theta*values
        weights = np.exp(logits-logits.max())
        return weights/weights.sum()
    theta = brentq(lambda z: float(np.dot(probabilities(z), values)-target), -20.0, 20.0)
    return probabilities(theta)


def red_zone_scoring_prior(model: PossessionModel, offense: float, opponent_defense: float) -> tuple[float,float]:
    values = np.array([offense,opponent_defense],dtype=float)
    values = np.where(np.isfinite(values),values,model.red_zone_mean)
    share = np.clip(model.scoring_share_intercept +
                    ((values-model.red_zone_mean)/model.red_zone_scale)@model.scoring_share_coef,.05,.95)
    scored = model.td_rate+model.fg_rate
    return float(scored*share),float(scored*(1-share))


def touchdown_points(td_count: np.ndarray, conversion: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Each TD is 6, 7 or 8, with historically estimated conversion rates."""
    n = np.asarray(td_count, dtype=int)
    seven = rng.binomial(n, conversion[1])
    remaining = n-seven
    denominator = conversion[0]+conversion[2]
    eight = rng.binomial(remaining, conversion[2]/denominator if denominator > 0 else 0.0)
    return 6*n + seven + 2*eight


def simulate_possession_scores(
    model: PossessionModel, expected_home: float, expected_away: float,
    home_pregame_drives: float = float("nan"), away_pregame_drives: float = float("nan"),
    *, draws: int = 20000, seed: int = 0, count_mode: str = "paired",
    scoring_mode: str = "empirical",
    home_red_zone: float = float("nan"), away_red_zone: float = float("nan"),
    home_opponent_def_red_zone: float = float("nan"),
    away_opponent_def_red_zone: float = float("nan"),
) -> dict:
    if draws < 1000:
        raise ValueError("At least 1,000 draws are needed for exact-score estimation")
    if count_mode not in {"paired", "poisson"}:
        raise ValueError("Unknown possession count candidate")
    if scoring_mode not in {"empirical", "red_zone"}:
        raise ValueError("Unknown scoring prior candidate")
    rng = np.random.default_rng(seed)
    base_drives = expected_drives(model, home_pregame_drives, away_pregame_drives)
    indexes = rng.integers(0, model.training_games, size=draws)
    if count_mode == "paired":
        drive_counts = np.rint(base_drives + model.drive_residual_pairs[indexes]).astype(int)
        drive_counts = np.clip(drive_counts, 1, 24)
    else:
        drive_counts = rng.poisson(base_drives, size=(draws,2))
    rare = model.rare_event_pairs[indexes]
    # Rare paired defensive/ST/safety events are historical team-game outcomes,
    # not attributes of the validation game. Same sampled game preserves coupling.
    rare_td = np.column_stack([rare[:,0]+rare[:,1], rare[:,4]+rare[:,5]])
    rare_points = np.column_stack([
        touchdown_points(rare_td[:,0], model.conversion_probabilities, rng)+2*rare[:,2]+2*rare[:,3],
        touchdown_points(rare_td[:,1], model.conversion_probabilities, rng)+2*rare[:,6]+2*rare[:,7],
    ])
    rare_mean = np.array([
        np.mean(model.rare_event_pairs[:,0]+model.rare_event_pairs[:,1])*np.dot([6,7,8],model.conversion_probabilities)+2*np.mean(model.rare_event_pairs[:,2]+model.rare_event_pairs[:,3]),
        np.mean(model.rare_event_pairs[:,4]+model.rare_event_pairs[:,5])*np.dot([6,7,8],model.conversion_probabilities)+2*np.mean(model.rare_event_pairs[:,6]+model.rare_event_pairs[:,7]),
    ])
    expected = np.array([expected_home, expected_away], dtype=float)
    if not np.isfinite(expected).all() or np.any(expected < 0):
        raise ValueError("Expected points must be finite and non-negative")
    scores = rare_points.copy()
    td_counts = np.zeros((draws,2),dtype=int)
    fg_counts = np.zeros((draws,2),dtype=int)
    for side in (0,1):
        if scoring_mode == "red_zone":
            offense = (home_red_zone,away_red_zone)[side]
            defense = (home_opponent_def_red_zone,away_opponent_def_red_zone)[side]
            prior_td,prior_fg = red_zone_scoring_prior(model,offense,defense)
        else:
            prior_td,prior_fg = model.td_rate,model.fg_rate
        no, fg, td = anchored_outcome_probabilities(model,expected[side],base_drives[side],rare_mean[side],prior_td,prior_fg)
        td_counts[:,side] = rng.binomial(drive_counts[:,side],td)
        remaining = drive_counts[:,side]-td_counts[:,side]
        fg_counts[:,side] = rng.binomial(remaining,fg/(fg+no))
        scores[:,side] += touchdown_points(td_counts[:,side],model.conversion_probabilities,rng)+3*fg_counts[:,side]
    maximum = int(max(70, scores.max(), model.feasible_scores.max()))
    grid = np.zeros((maximum+1, maximum+1),dtype=float)
    np.add.at(grid,(scores[:,0],scores[:,1]),1)
    grid /= draws
    # Tiny arithmetic-support pseudocount prevents Monte-Carlo zero likelihoods;
    # it is NOT a prior fitted to common final-score numbers.
    support = model.feasible_scores[model.feasible_scores <= maximum]
    prior = np.zeros(maximum+1)
    prior[support] = 1/len(support)
    grid = 0.999*grid + 0.001*np.outer(prior,prior)
    probability = joint_distribution({"base":grid},expected_home,expected_away)
    summary = score_summary(probability)
    home_values = np.arange(probability.shape[0])[:,None]
    away_values = np.arange(probability.shape[1])[None,:]
    summary.update({
        "joint_probability":probability,
        "win_probability_home":float(probability[home_values>away_values].sum()+0.5*probability[home_values==away_values].sum()),
        "possession_home_mean":float(drive_counts[:,0].mean()),
        "possession_away_mean":float(drive_counts[:,1].mean()),
        "possession_correlation":float(np.corrcoef(drive_counts.T)[0,1]),
        "possession_home_p10_p90":np.quantile(drive_counts[:,0],[.1,.9]).tolist(),
        "possession_away_p10_p90":np.quantile(drive_counts[:,1],[.1,.9]).tolist(),
        "simulated_outcome_reconciles":bool(np.all(td_counts+fg_counts <= drive_counts)),
        "expected_home_points":float(expected_home),
        "expected_away_points":float(expected_away),
        "raw_home_mean":float(scores[:,0].mean()),
        "raw_away_mean":float(scores[:,1].mean()),
    })
    return summary
