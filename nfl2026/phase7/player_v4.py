"""Opportunity-first V4 research on an explicitly uncertified roster cohort."""
from __future__ import annotations

import time
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits
from scipy.special import expit, logit

from nfl2026.features.player_rollforward import roll_player_usage_features
from nfl2026.five_week_review import historical_players, immutable_research_artifact, finite, block_bootstrap_difference
from nfl2026.phase7.common import json_write

VERSION = "phase7-player-v4-opportunity-research-v1"
KEYS = ["game_id", "player_id", "team", "season", "week"]


def population_mask(frame, target):
    if target in {"carries", "rushing_yards", "red_zone_carries", "inside_5_carries"}:
        return frame.position.isin(["QB", "RB", "FB"])
    if target == "snap_share":
        return pd.Series(True, index=frame.index)
    return frame.position.ne("QB")


def reconcile_candidate_td(probabilities, target_sum, tolerance=1e-8):
    """Experimental numerical repair: return the evaluated converged root.

    Incumbent updates a bracket, breaks, then returns an unevaluated midpoint.
    That can violate its stated tolerance. Production code is NOT changed here.
    """
    p = np.asarray(probabilities, float)
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any() or tolerance <= 0 or not np.isfinite(target_sum):
        raise ValueError("Invalid TD reconciliation contract")
    target = float(np.clip(target_sum, 0, len(p)))
    if not len(p) or target == 0:
        return np.zeros_like(p)
    if target == len(p):
        return np.ones_like(p)
    base = logit(np.clip(p, 1e-6, 1-1e-6))
    low, high = -30., 30.
    for _ in range(100):
        midpoint = (low+high)/2
        candidate = expit(base+midpoint)
        delta = candidate.sum()-target
        if abs(delta) <= tolerance:
            return candidate
        if delta < 0:
            low = midpoint
        else:
            high = midpoint
    raise ValueError("Candidate TD reconciliation did not converge")


def refresh_optional_metrics(settings, predictions):
    """Recompute reporting cohorts from archived OOS rows, without refitting."""
    path = settings.report_root / "phase7_player_v4_metrics.csv"
    optional = {"receptions", "snap_share", "red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets"}
    prior = pd.read_csv(path)
    rows = []
    for (model, season), group in predictions.groupby(["model", "season"]):
        for target in sorted(optional):
            part = group[population_mask(group, target) & group[f"actual_{target}"].notna()]
            if part.empty:
                continue
            error = part[f"pred_{target}"] - part[f"actual_{target}"]
            rows.append({"model":model,"season":int(season),"target":target,"prediction":f"pred_{target}",
                "rows":len(part),"mae":float(error.abs().mean()),"rmse":float(np.sqrt(np.mean(error**2))),
                "bias":float(error.mean()),"eligibility_certified":False})
    pd.concat([prior[~prior.target.isin(optional)],pd.DataFrame(rows)],ignore_index=True).to_csv(path,index=False)


def td_reconciliation_diagnostics(settings, predictions):
    """Reuse incumbent scoring-volume reconciliation; do not promote raw classifiers."""
    from nfl2026.models.player_advanced import reconcile_td_probabilities
    from nfl2026.five_week_review import probability_metrics
    reference = pd.read_csv(settings.artifact_root / "research/five_week_review/td_incumbent_universe_replay.csv")
    teams = pd.read_csv(settings.artifact_root / "backtests/phase2_team_predictions.csv")
    teams = teams[teams.model.eq("extra_trees")]
    points = {(r.game_id, r.home_team): r.pred_home_score for r in teams.itertuples()}
    points.update({(r.game_id, r.away_team): r.pred_away_score for r in teams.itertuples()})
    rows = []
    integrity = []
    for model, group in predictions.groupby("model"):
        d = group.merge(reference[KEYS + ["raw_probability", "reconciled_probability"]], on=KEYS, validate="one_to_one").reset_index(drop=True)
        d["candidate_reconciled_probability"] = np.nan
        for (game, team), indices in d.groupby(["game_id", "team"]).groups.items():
            budget = float(np.clip(points[(game, team)] / 7 * .72, .35, 4.25))
            raw = d.loc[indices, "raw_td_probability"].to_numpy()
            incumbent_numerical_result = reconcile_td_probabilities(raw, budget)
            p = reconcile_candidate_td(raw, budget)
            if abs(p.sum() - budget) > 1e-6:
                raise ValueError("TD reconciliation failed")
            d.loc[indices, "candidate_reconciled_probability"] = p
            integrity.append({"model":model,"game_id":game,"team":team,"budget":budget,"probability_sum":float(p.sum()),
                "incumbent_algorithm_sum_error":float(incumbent_numerical_result.sum()-min(budget,len(raw))),
                "candidate_sum_error":float(p.sum()-min(budget,len(raw))), "method":"EXPERIMENTAL_CONVERGED_ROOT_REPAIR"})
        for (season, position), s in d.groupby(["season", "position"]):
            for label, column in {"incumbent_raw":"raw_probability", "incumbent_reconciled":"reconciled_probability",
                "candidate_raw":"raw_td_probability", "candidate_reconciled":"candidate_reconciled_probability"}.items():
                rows.append({"model":model,"season":int(season),"position":position,"method":label,"rows":len(s),
                    **probability_metrics(s.actual_anytime_offensive_td, s[column]), "eligibility_certified":False})
    pd.DataFrame(rows).to_csv(settings.report_root / "phase7_td_reconciliation_metrics.csv", index=False)
    pd.DataFrame(integrity).to_csv(settings.report_root / "phase7_td_reconciliation_budgets.csv", index=False)


def allocate_opportunities(frame: pd.DataFrame, value: str, budget: str) -> np.ndarray:
    """Research variant: allocate an explicitly supplied team budget, not invented plays.

    Zero weights retain an unallocated budget instead of equal synthetic workloads.
    Callers must validate the team volume forecast; production does not call this.
    """
    values = pd.to_numeric(frame[value], errors="raise").clip(lower=0)
    totals = values.groupby([frame.game_id, frame.team]).transform("sum")
    supplied = pd.to_numeric(frame[budget], errors="coerce")
    if supplied.groupby([frame.game_id, frame.team]).nunique().gt(1).any():
        raise ValueError("Conflicting team opportunity budgets")
    if supplied.lt(0).any():
        raise ValueError("Negative team opportunity budget")
    return np.where(supplied.notna() & totals.gt(0), values / totals.replace(0, np.nan) * supplied, values)


def build_research_panel(settings):
    conditional, panel, features = historical_players(settings)
    observations = conditional.rename(columns={f"target_{t}": t for t in
        ("carries", "targets", "rushing_yards", "receiving_yards", "anytime_offensive_td")})
    panel = roll_player_usage_features(observations, panel)
    # New targets are outcomes only, never part of the feature list.
    extras = conditional[KEYS + ["red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets", "offense_pct"]]
    panel = panel.merge(extras, on=KEYS, how="left", validate="one_to_one")
    for name in ("red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets"):
        panel[f"actual_{name}"] = panel[name].fillna(0)
    # Do not label an unmatched snap row as measured zero; restrict snap evaluation.
    panel["actual_snap_share"] = panel.offense_pct
    weekly = pd.read_csv(settings.artifact_root / "canonical/player_weekly.csv", low_memory=False)
    weekly = weekly[weekly.player_id.notna() & weekly.season.lt(2026)]
    if weekly.duplicated(["season", "week", "player_id", "team"]).any():
        raise ValueError("Receptions weekly identity duplicated")
    panel = panel.merge(weekly[["season", "week", "player_id", "team", "receptions"]],
                        on=["season", "week", "player_id", "team"], how="left", validate="one_to_one")
    panel["actual_receptions"] = panel.receptions.where(panel.actual_participant, 0)
    panel["actual_anytime_offensive_td"] = panel.actual_anytime_offensive_td.fillna(0)
    if any(c.startswith("actual_") or c.startswith("target_") for c in features):
        raise ValueError("Outcome feature leakage")
    return panel, features


def estimator(kind, seed):
    base = ExtraTreesRegressor(n_estimators=100, max_depth=8, min_samples_leaf=12,
            max_features=.75, n_jobs=2, random_state=seed) if kind == "extra_trees" else Ridge(alpha=20)
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), base)


def fit_catch_rate(train: pd.DataFrame, features: list[str]):
    """Binomial logistic with aggregate successes/failures, not current-game ratios as X."""
    valid = train.actual_targets.gt(0) & train.actual_receptions.notna()
    data = train.loc[valid]
    if data.actual_receptions.gt(data.actual_targets).any() or data.actual_receptions.lt(0).any():
        raise ValueError("Historical reception/target arithmetic inconsistent")
    successes = data.actual_receptions.to_numpy()
    failures = data.actual_targets.to_numpy() - successes
    x = pd.concat([data[features], data[features]], ignore_index=True)
    y = np.r_[np.ones(len(data)), np.zeros(len(data))]
    weights = np.r_[successes, failures]
    model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=.25, max_iter=600))
    model.fit(x.loc[weights > 0], y[weights > 0], logisticregression__sample_weight=weights[weights > 0])
    return model


def evaluate_candidates(settings) -> dict:
    cache = settings.artifact_root / "research/phase7"
    output = cache / "player_v4_oos.csv"
    decision_path = settings.report_root / "phase7_player_v4_decision.json"
    if output.exists() and decision_path.exists():
        import json
        saved = pd.read_csv(output)
        refresh_optional_metrics(settings, saved)
        td_reconciliation_diagnostics(settings, saved)
        result = json.loads(decision_path.read_text())
        result["TD_reconciliation"] = "same incumbent scoring budget; experimental converged-root numerical repair tested; production unchanged"
        json_write(decision_path, result)
        return result
    started = time.perf_counter()
    panel, features = build_research_panel(settings)
    parts = []
    with threadpool_limits(2):
        for season in (2023, 2024, 2025):
            train, test = panel[panel.season.lt(season)].copy(), panel[panel.season.eq(season)].copy()
            maximum = pd.to_datetime(train.kickoff_utc, utc=True).max()
            if maximum >= pd.to_datetime(test.kickoff_utc, utc=True).min():
                raise ValueError("Training extends into target fold")
            for kind in ("ridge", "extra_trees"):
                print(f"Player V4 research {kind}: train through {season-1}, evaluate {season}", flush=True)
                d = test[KEYS + ["position", "player_name", "kickoff_utc", "actual_participant"]
                         + [c for c in test if c.startswith("actual_")]].copy()
                models = {}
                for target in ("carries", "targets", "red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets", "snap_share"):
                    mask = train[f"actual_{target}"].notna()
                    models[target] = estimator(kind, 20261014).fit(train.loc[mask, features], train.loc[mask, f"actual_{target}"])
                    prediction = models[target].predict(test[features])
                    d[f"pred_{target}"] = np.clip(prediction, 0, 1 if target == "snap_share" else None)
                for target, opportunity in (("rushing_yards", "carries"), ("receiving_yards", "targets")):
                    direct = estimator(kind, 20261014).fit(train[features], train[f"actual_{target}"])
                    d[f"direct_{target}"] = np.maximum(0, direct.predict(test[features]))
                    mask = train[f"actual_{opportunity}"].gt(0)
                    efficiency = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=30))
                    efficiency.fit(train.loc[mask, features], train.loc[mask, f"actual_{target}"] / train.loc[mask, f"actual_{opportunity}"],
                                   ridge__sample_weight=train.loc[mask, f"actual_{opportunity}"])
                    d[f"pred_{target}"] = d[f"pred_{opportunity}"] * efficiency.predict(test[features])
                    models[target] = {"direct": direct, "efficiency": efficiency}
                catch = fit_catch_rate(train, features)
                d["catch_probability"] = catch.predict_proba(test[features])[:, 1]
                d["pred_receptions"] = d.pred_targets * d.catch_probability
                models["catch"] = catch
                td = np.zeros(len(test))
                for group, positions in {"QB": ["QB"], "RB_FB": ["RB", "FB"], "WR_TE": ["WR", "TE"]}.items():
                    tr = train.position.isin(positions)
                    te = test.position.isin(positions).to_numpy()
                    # QB-specific ANYTIME OFFENSIVE TD uses lagged rushing/goal-line signal,
                    # never passing TD targets, receiver opportunity or retrospective starter identity.
                    inputs = [c for c in features if not any(t in c for t in ("targets", "receiving", "snap", "position_"))] if group == "QB" else features
                    model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=.2, max_iter=800))
                    model.fit(train.loc[tr, inputs], train.loc[tr, "actual_anytime_offensive_td"])
                    td[te] = model.predict_proba(test.loc[test.position.isin(positions), inputs])[:, 1]
                    models[f"td_{group}"] = {"model": model, "features": inputs}
                d["raw_td_probability"] = td
                d["max_training_kickoff_utc"] = maximum.isoformat()
                d["model"] = f"{VERSION}-{kind}"
                d["validation_status"] = "EXPERIMENTAL_UNCERTIFIED_ELIGIBILITY"
                # Prior team context is an experimental volume budget, not an official team play prediction.
                allocation = test[["game_id", "team", "recent_team_carries", "recent_team_targets"]].reset_index(drop=True)
                for target in ("carries", "targets"):
                    allocation["weight"] = d[f"pred_{target}"].to_numpy()
                    d[f"allocated_{target}"] = allocate_opportunities(allocation, "weight", f"recent_team_{target}")
                parts.append(d)
                immutable_research_artifact(cache / "models" / f"player-v4-{kind}-train-through-{season-1}.joblib",
                    {"models": models, "features": features}, {"version": VERSION, "algorithm": kind,
                        "max_training_kickoff_utc": maximum.isoformat(), "training_seasons": sorted(train.season.unique().tolist()),
                        "feature_schema": features, "eligibility_certified": False, "production_registered": False})
    predictions = pd.concat(parts, ignore_index=True)
    predictions.to_csv(output, index=False)
    reference = pd.read_csv(settings.artifact_root / "research/five_week_review/player_production_semantics_replay.csv")
    metrics = []
    comparisons = []
    for (model, season), group in predictions.groupby(["model", "season"]):
        shared = group.merge(reference[KEYS + [f"pred_{t}" for t in ("carries", "targets", "rushing_yards", "receiving_yards")]],
                             on=KEYS, validate="one_to_one", suffixes=("", "_champion"))
        for field in ("carries", "targets", "rushing_yards", "receiving_yards", "receptions", "snap_share", "red_zone_carries", "inside_5_carries", "red_zone_targets", "inside_5_targets"):
            population = population_mask(shared, field)
            valid = population & shared[f"actual_{field}"].notna()
            s = shared.loc[valid]
            if not len(s):
                continue
            variants = [f"pred_{field}"]
            if field.endswith("yards"):
                variants.append(f"direct_{field}")
            if field in {"carries", "targets"}:
                variants.append(f"allocated_{field}")
            for prediction in variants:
                err = s[prediction] - s[f"actual_{field}"]
                row = {"model": model, "season": int(season), "target": field, "prediction": prediction,
                       "rows": len(s), "mae": float(err.abs().mean()), "rmse": float(np.sqrt(np.mean(err**2))),
                       "bias": float(err.mean()), "eligibility_certified": False}
                if f"pred_{field}_champion" in s:
                    referr = s[f"pred_{field}_champion"] - s[f"actual_{field}"]
                    delta, lo, hi = block_bootstrap_difference(s, err.abs().to_numpy() - referr.abs().to_numpy(), 500)
                    row.update({"champion_mae": float(referr.abs().mean()), "mae_delta": delta,
                                "delta_ci_low": lo, "delta_ci_high": hi})
                metrics.append(row)
        for position, s in shared.groupby("position"):
            comparisons.append({"model": model, "season": int(season), "position": position, "rows": len(s),
                "raw_td_brier": float(np.mean((s.raw_td_probability - s.actual_anytime_offensive_td)**2))})
    pd.DataFrame(metrics).to_csv(settings.report_root / "phase7_player_v4_metrics.csv", index=False)
    pd.DataFrame(comparisons).to_csv(settings.report_root / "phase7_player_v4_td_metrics.csv", index=False)
    td_reconciliation_diagnostics(settings, predictions)
    result = {"status": "TESTED_CANDIDATES_NOT_PROMOTED", "oos_rows_per_candidate": int(len(predictions) / 2),
        "algorithms": ["ridge", "extra_trees"], "folds": [2023, 2024, 2025], "seconds": time.perf_counter() - started,
        "selection": "NO_PRODUCTION_SELECTION", "promotion": False,
        "reason": "Historical eligibility and role-change publication are not certified; no automatic promotion.",
        "active_probability": "BLOCKED_BY_HISTORICAL_ELIGIBILITY", "QB_passing": "NOT_IMPLEMENTED_YET",
        "snap_share": "measured rows only, season-specific missing coverage reported", "role_changes": "BLOCKED_BY_HISTORICAL_PREGAME_ROLE_PROVENANCE",
        "TD_reconciliation": "incumbent retained; new position-model probabilities evaluated raw only"}
    json_write(decision_path, finite(result))
    return result
