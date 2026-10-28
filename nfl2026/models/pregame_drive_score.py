"""Research-only, pregame-conditioned drive-count/outcome score simulator.

All feature names are an explicit allowlist from the shifted historical Phase 2
matrix. The current game's drive/score outcomes enter targets, never features.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor, HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, PoissonRegressor, Ridge
from sklearn.multioutput import MultiOutputRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from nfl2026.models.possession_score import fit_possession_model, touchdown_points
from nfl2026.models.score_distribution import score_summary


OUTCOMES = ("TD", "FG", "TURNOVER", "OTHER_NO_SCORE")
OFFENSE_METRICS = (
    "drives", "plays", "points_for", "epa_per_play", "pass_epa", "rush_epa",
    "success_rate", "explosive_rate", "red_zone_td_rate", "neutral_pass_rate",
    "pass_rate", "turnover_rate", "sack_rate",
)
DEFENSE_METRICS = (
    "epa_per_play", "pass_epa", "rush_epa", "success_rate", "explosive_rate",
    "red_zone_td_rate", "turnover_rate", "sack_rate", "pressure_rate",
)
INTERACTION_PAIRS = (
    ("off_pass_epa", "opp_def_pass_epa", "pass_epa_x_defense"),
    ("off_rush_epa", "opp_def_rush_epa", "rush_epa_x_defense"),
    ("off_success_rate", "opp_def_success_rate", "success_x_defense"),
    ("off_explosive_rate", "opp_def_explosive_rate", "explosive_x_defense"),
    ("off_red_zone_td_rate", "opp_def_red_zone_td_rate", "red_zone_x_defense"),
    ("off_drives", "opponent_drives", "pace_x_opponent_pace"),
)
TEAM_MAIN = tuple([f"off_{m}" for m in OFFENSE_METRICS] +
                  [f"opp_def_{m}" for m in DEFENSE_METRICS] +
                  ["opponent_drives", "opponent_plays", "own_elo", "opponent_elo",
                   "rest_days", "opponent_rest_days", "is_home", "week_number",
                   "roof_closed_or_dome", "neutral_site"])
TEAM_INTERACTIONS = tuple(x[2] for x in INTERACTION_PAIRS)
COUNT_FEATURES = (
    "home_pregame_drives", "away_pregame_drives", "home_pregame_plays",
    "away_pregame_plays", "home_pregame_neutral_pass_rate",
    "away_pregame_neutral_pass_rate", "home_pregame_epa_per_play",
    "away_pregame_epa_per_play", "home_pregame_def_epa_per_play",
    "away_pregame_def_epa_per_play", "home_rest_days", "away_rest_days",
    "roof_closed_or_dome", "neutral_site", "week_number",
    "home_away_pace_product",
)


def build_game_features(matrix: pd.DataFrame) -> pd.DataFrame:
    """Never select target/current-game columns by wildcard."""
    selected = ["game_id", "season", "week", *[c for c in COUNT_FEATURES if c != "home_away_pace_product"]]
    missing = sorted(set(selected)-set(matrix))
    if missing:
        raise ValueError(f"PIT count features missing: {missing}")
    out = matrix[selected].copy()
    out["home_away_pace_product"] = out.home_pregame_drives * out.away_pregame_drives
    return out


def build_team_features(matrix: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for side, opponent in (("home", "away"), ("away", "home")):
        out = matrix[["game_id", "season", "week"]].copy()
        out["side"] = side.upper()
        for metric in OFFENSE_METRICS:
            out[f"off_{metric}"] = matrix[f"{side}_pregame_{metric}"].to_numpy()
        for metric in DEFENSE_METRICS:
            out[f"opp_def_{metric}"] = matrix[f"{opponent}_pregame_def_{metric}"].to_numpy()
        out["opponent_drives"] = matrix[f"{opponent}_pregame_drives"].to_numpy()
        out["opponent_plays"] = matrix[f"{opponent}_pregame_plays"].to_numpy()
        out["own_elo"] = matrix[f"{side}_elo"].to_numpy()
        out["opponent_elo"] = matrix[f"{opponent}_elo"].to_numpy()
        out["rest_days"] = matrix[f"{side}_rest_days"].to_numpy()
        out["opponent_rest_days"] = matrix[f"{opponent}_rest_days"].to_numpy()
        out["is_home"] = float(side == "home")
        out["week_number"] = matrix.week_number.to_numpy()
        out["roof_closed_or_dome"] = matrix.roof_closed_or_dome.to_numpy()
        out["neutral_site"] = matrix.neutral_site.to_numpy()
        for offense, defense, name in INTERACTION_PAIRS:
            out[name] = out[offense] * out[defense]
        rows.append(out)
    return pd.concat(rows, ignore_index=True)


def training_frames(history: pd.DataFrame, matrix: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    valid = history[history.research_valid.astype(bool)].copy()
    games = build_game_features(matrix)
    sides = valid.pivot(index="game_id", columns="side", values="offensive_possessions")
    sides.columns = [f"actual_{c.lower()}_drives" for c in sides.columns]
    games = games.merge(sides.reset_index(), on="game_id", how="inner", validate="one_to_one")
    games = games.dropna(subset=["actual_home_drives", "actual_away_drives"]).sort_values("game_id").reset_index(drop=True)
    team = build_team_features(matrix).merge(
        valid[["game_id", "side", "offensive_possessions", "td_possessions", "fg_possessions",
               "turnover_possessions", "no_score_possessions"]],
        on=["game_id", "side"], how="inner", validate="one_to_one")
    team = team[team.game_id.isin(games.game_id)].sort_values(["game_id", "side"]).reset_index(drop=True)
    # TD/FG override incidental turnover flags on scoring drives. No-score is
    # partitioned into turnover and other outcomes, exactly once per drive.
    team["turnover_drives"] = np.minimum(team.turnover_possessions, team.no_score_possessions)
    team["other_no_score_drives"] = team.no_score_possessions-team.turnover_drives
    counts = team[["td_possessions", "fg_possessions", "turnover_drives", "other_no_score_drives"]].to_numpy()
    if not np.array_equal(counts.sum(axis=1), team.offensive_possessions.to_numpy()):
        raise ValueError("Drive-outcome target counts do not reconcile")
    if (counts < 0).any():
        raise ValueError("Negative drive-outcome target")
    return games, team


def count_estimator(name: str):
    if name == "historical_mean":
        return make_pipeline(SimpleImputer(strategy="median"), DummyRegressor(strategy="mean"))
    if name == "ridge":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=80.0))
    if name == "poisson":
        head = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), PoissonRegressor(alpha=4.0, max_iter=800))
        return MultiOutputRegressor(head)
    if name == "extra_trees":
        return make_pipeline(SimpleImputer(strategy="median"), ExtraTreesRegressor(n_estimators=120, max_depth=5, min_samples_leaf=18, max_features=.8, n_jobs=-1, random_state=26))
    if name == "hist_gradient_boosting":
        head = make_pipeline(SimpleImputer(strategy="median"), HistGradientBoostingRegressor(max_iter=120, learning_rate=.035, max_leaf_nodes=9, min_samples_leaf=24, l2_regularization=8.0, random_state=26))
        return MultiOutputRegressor(head)
    raise ValueError(f"Unknown count model: {name}")


def outcome_estimator(name: str):
    if name == "historical_prior":
        return make_pipeline(SimpleImputer(strategy="median"), DummyClassifier(strategy="prior"))
    if name in {"logistic_main", "logistic_interactions"}:
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=.05, max_iter=800))
    if name in {"extra_trees_main", "extra_trees_interactions"}:
        return make_pipeline(SimpleImputer(strategy="median"), ExtraTreesClassifier(n_estimators=150, max_depth=6, min_samples_leaf=22, max_features=.75, n_jobs=-1, random_state=26))
    if name == "hist_gradient_boosting_interactions":
        return make_pipeline(SimpleImputer(strategy="median"), HistGradientBoostingClassifier(max_iter=120, learning_rate=.035, max_leaf_nodes=9, min_samples_leaf=35, l2_regularization=10.0, random_state=26))
    raise ValueError(f"Unknown outcome model: {name}")


def outcome_features(name: str) -> tuple[str, ...]:
    return TEAM_MAIN if name in {"historical_prior", "logistic_main", "extra_trees_main"} else TEAM_MAIN + TEAM_INTERACTIONS


def fit_outcome(name: str, team: pd.DataFrame):
    features = outcome_features(name)
    counts = team[["td_possessions", "fg_possessions", "turnover_drives", "other_no_score_drives"]].to_numpy(dtype=float)
    x = pd.DataFrame(np.repeat(team[list(features)].to_numpy(), 4, axis=0), columns=features)
    labels = np.tile(np.arange(4), len(team))
    weights = counts.reshape(-1)
    estimator = outcome_estimator(name)
    estimator.fit(x, labels, **{"dummyclassifier__sample_weight" if name == "historical_prior" else
                                "logisticregression__sample_weight" if name.startswith("logistic") else
                                "extratreesclassifier__sample_weight" if name.startswith("extra") else
                                "histgradientboostingclassifier__sample_weight": weights})
    return estimator


@dataclass
class PregameDriveModel:
    count_name: str
    outcome_name: str
    count_model: object
    outcome_model: object
    count_residual_pairs: np.ndarray
    rare_event_pairs: np.ndarray
    conversion_probabilities: np.ndarray
    feasible_scores: np.ndarray
    training_seasons: tuple[int, ...]
    training_games: int


def fit_pregame_drive_model(history: pd.DataFrame, matrix: pd.DataFrame, count_name: str, outcome_name: str) -> PregameDriveModel:
    games, team = training_frames(history, matrix)
    if len(games) < 100:
        raise ValueError("Insufficient reconciled historical games")
    count = count_estimator(count_name)
    y = games[["actual_home_drives", "actual_away_drives"]].to_numpy(dtype=float)
    count.fit(games[list(COUNT_FEATURES)], y)
    residual = y-np.asarray(count.predict(games[list(COUNT_FEATURES)]),dtype=float)
    outcome = fit_outcome(outcome_name, team)
    prior = fit_possession_model(history[history.game_id.isin(games.game_id)], matrix[matrix.game_id.isin(games.game_id)])
    # Both the new game frame and prior pairing are sorted by game_id.
    if len(prior.rare_event_pairs) != len(games):
        raise ValueError("Rare-event and drive residual pairs are not aligned")
    return PregameDriveModel(count_name, outcome_name, count, outcome, residual,
                             prior.rare_event_pairs, prior.conversion_probabilities,
                             prior.feasible_scores, tuple(sorted(games.season.unique())), len(games))


def predict_components(model: PregameDriveModel, matrix_row: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    if len(matrix_row) != 1:
        raise ValueError("Prediction must be game keyed")
    game = build_game_features(matrix_row)
    team = build_team_features(matrix_row).set_index("side").loc[["HOME", "AWAY"]].reset_index()
    drives = np.clip(np.asarray(model.count_model.predict(game[list(COUNT_FEATURES)])[0],dtype=float),1,24)
    raw = model.outcome_model.predict_proba(team[list(outcome_features(model.outcome_name))])
    probabilities = np.clip(np.asarray(raw,dtype=float),1e-6,1)
    probabilities /= probabilities.sum(axis=1,keepdims=True)
    return drives, probabilities


def anchor_probabilities(probabilities: np.ndarray, expected_points: float, drives: float,
                         rare_mean: float, td_value: float, mode: str) -> tuple[np.ndarray,float]:
    """Adjust drive scoring *rates*, never integer score draws or final grid."""
    p = np.clip(np.asarray(probabilities,dtype=float),1e-8,1)
    p /= p.sum()
    if mode == "unanchored":
        return p,0.0
    if mode not in {"gentle", "blend"}:
        raise ValueError("Unknown expected-point anchoring mode")
    values=np.array([td_value,3.0,0.0,0.0])
    target=float(np.clip((expected_points-rare_mean)/drives,.001,td_value-.001))
    def tilt(theta: float):
        x=np.log(p)+theta*values
        y=np.exp(x-x.max())
        return y/y.sum()
    full=brentq(lambda theta: float(np.dot(tilt(theta),values)-target),-20,20)
    result=tilt(.5*full) if mode=="gentle" else .5*p+.5*tilt(full)
    return result,float(full if mode=="blend" else .5*full)


def headline_decisions(p: np.ndarray, expected_home: float, expected_away: float) -> dict[str,tuple[int,int]]:
    if p.ndim != 2 or p.shape[0] != p.shape[1]:
        raise ValueError("A square joint score grid is required")
    values=np.arange(p.shape[0])
    positive=p>0
    h,a=np.unravel_index(np.argmax(p),p.shape)
    home=p.sum(axis=1); away=p.sum(axis=0)
    home_cost=np.abs(values[:,None]-values[None,:])@home
    away_cost=np.abs(values[:,None]-values[None,:])@away
    cost=home_cost[:,None]+away_cost[None,:]
    mh,ma=np.unravel_index(np.argmin(np.where(positive,cost,np.inf)),p.shape)
    mean_h,mean_a=float(values@home),float(values@away)
    squared=(values[:,None]-mean_h)**2+(values[None,:]-mean_a)**2
    sh,sa=np.unravel_index(np.argmin(np.where(positive,squared,np.inf)),p.shape)
    return {
        "rounded_continuous":(round(expected_home),round(expected_away)),
        "joint_mode":(int(h),int(a)),
        "joint_l1_medoid":(int(mh),int(ma)),
        "joint_squared_loss":(int(sh),int(sa)),
    }


def simulate_pregame_game(model: PregameDriveModel, matrix_row: pd.DataFrame,
                          expected_home: float, expected_away: float, *,
                          anchor_mode: str = "gentle", draws: int = 30000, seed: int = 0) -> dict:
    if draws < 1000:
        raise ValueError("At least 1,000 draws required")
    rng=np.random.default_rng(seed)
    count_mean, raw_prob=predict_components(model,matrix_row)
    idx=rng.integers(0,model.training_games,size=draws)
    counts=np.clip(np.rint(count_mean+model.count_residual_pairs[idx]).astype(int),1,24)
    rare=model.rare_event_pairs[idx]
    conversion=model.conversion_probabilities
    td_value=float(np.dot([6,7,8],conversion))
    rare_mean=np.array([
        np.mean(model.rare_event_pairs[:,0]+model.rare_event_pairs[:,1])*td_value+2*np.mean(model.rare_event_pairs[:,2]+model.rare_event_pairs[:,3]),
        np.mean(model.rare_event_pairs[:,4]+model.rare_event_pairs[:,5])*td_value+2*np.mean(model.rare_event_pairs[:,6]+model.rare_event_pairs[:,7]),
    ])
    rare_td=np.column_stack([rare[:,0]+rare[:,1],rare[:,4]+rare[:,5]])
    scores=np.column_stack([
        touchdown_points(rare_td[:,0],conversion,rng)+2*rare[:,2]+2*rare[:,3],
        touchdown_points(rare_td[:,1],conversion,rng)+2*rare[:,6]+2*rare[:,7],
    ])
    probabilities=[]; adjustments=[]; outcomes=[]
    for side, expected in enumerate((expected_home,expected_away)):
        p, adjustment=anchor_probabilities(raw_prob[side],expected,count_mean[side],rare_mean[side],td_value,anchor_mode)
        o=rng.multinomial(counts[:,side],p)
        scores[:,side]+=touchdown_points(o[:,0],conversion,rng)+3*o[:,1]
        probabilities.append(p)
        adjustments.append(adjustment)
        outcomes.append(o)
    maximum=int(max(70,scores.max(),model.feasible_scores.max()))
    grid=np.zeros((maximum+1,maximum+1),dtype=float)
    np.add.at(grid,(scores[:,0],scores[:,1]),1)
    grid/=draws
    support=model.feasible_scores[model.feasible_scores<=maximum]
    prior=np.zeros(maximum+1)
    prior[support]=1/len(support)
    grid=.999*grid+.001*np.outer(prior,prior)
    summary=score_summary(grid)
    values=np.arange(grid.shape[0]); h=values[:,None]; a=values[None,:]
    summary.update({
        "joint_probability":grid,
        "headline_rules":headline_decisions(grid,expected_home,expected_away),
        "win_probability_home":float(grid[h>a].sum()+.5*grid[h==a].sum()),
        "expected_home_points":float(expected_home),"expected_away_points":float(expected_away),
        "expected_home_drives":float(count_mean[0]),"expected_away_drives":float(count_mean[1]),
        "raw_home_td_probability":float(raw_prob[0,0]),"raw_away_td_probability":float(raw_prob[1,0]),
        "raw_home_fg_probability":float(raw_prob[0,1]),"raw_away_fg_probability":float(raw_prob[1,1]),
        "used_home_td_probability":float(probabilities[0][0]),"used_away_td_probability":float(probabilities[1][0]),
        "used_home_fg_probability":float(probabilities[0][1]),"used_away_fg_probability":float(probabilities[1][1]),
        "home_anchor_logit_adjustment":adjustments[0],"away_anchor_logit_adjustment":adjustments[1],
        "home_possession_mean":float(counts[:,0].mean()),"away_possession_mean":float(counts[:,1].mean()),
        "possession_correlation":float(np.corrcoef(counts.T)[0,1]),
        "outcome_arithmetic_valid":bool(all(np.array_equal(o.sum(axis=1),counts[:,i]) for i,o in enumerate(outcomes))),
    })
    return summary
