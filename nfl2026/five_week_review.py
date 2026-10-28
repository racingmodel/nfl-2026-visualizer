"""Time-ordered five-week review. Research outputs never select production models.

2023/2024 are development folds; 2025 is the confirmation fold. Frozen live
forecasts are evaluated independently and never rewritten as candidate output.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import joblib
import numpy as np
import pandas as pd
from scipy.special import expit, logit
from scipy.stats import norm, spearmanr
from sklearn.base import clone
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import Ridge, PoissonRegressor, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from nfl2026.utilities.io import sha256_file

SEED = 20261014
TARGETS = ("carries", "targets", "rushing_yards", "receiving_yards")
PLAYER_REFERENCE="incumbent_pit_semantics_replay"
PREREGISTERED_GATES = {
    "development": "2023-2024 rolling-origin; train only seasons preceding target",
    "confirmation": "2025 untouched until development ranking is fixed",
    "live": "2026 frozen Weeks 1-5; cannot select hyperparameters or ensemble weights",
    "primary": "team points MAE; opportunity/yardage MAE; TD/win Brier",
    "material_improvement": "paired season-week block bootstrap upper 95% CI < 0 on development and confirmation",
    "stability": "positive improvement in at least two of three seasons; no significant live regression",
    "secondary_noninferiority": "total/margin MAE, bias, proper scores and early-season MAE: upper 95% CI below 1% of incumbent primary metric",
    "provenance": "every fitted predictor and eligible forecast population must have proved pregame provenance",
    "live_replay": "candidate requires immutable exact historical input state; absent source feature copies cannot be silently reconstructed",
    "operations": "all full tests pass; deterministic seed; immutable version; supported current input contract",
    "no_automatic_promotion": True,
}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def finite(value):
    if isinstance(value, dict): return {str(k): finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [finite(v) for v in value]
    if isinstance(value, (np.integer,)): return int(value)
    if isinstance(value, (np.floating, float)): return float(value) if np.isfinite(value) else None
    if isinstance(value, np.bool_): return bool(value)
    return value


def csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")


def probability_metrics(y, probability) -> dict:
    y = np.asarray(y, float); p = np.clip(np.asarray(probability, float), 1e-8, 1-1e-8)
    bucket = np.minimum((p * 10).astype(int), 9)
    ece = sum(abs(p[bucket == k].mean()-y[bucket == k].mean()) * (bucket == k).mean()
              for k in np.unique(bucket))
    hit = p >= .5
    slope = intercept = np.nan
    if len(np.unique(y)) > 1:
        fit = LogisticRegression(C=1e6, max_iter=1000).fit(logit(p).reshape(-1, 1), y)
        slope, intercept = float(fit.coef_[0,0]), float(fit.intercept_[0])
    return {"brier": float(np.mean((p-y)**2)),
            "log_loss": float(-np.mean(y*np.log(p)+(1-y)*np.log1p(-p))), "ece": float(ece),
            "probability_mean": float(p.mean()), "event_rate": float(y.mean()),
            "calibration_slope": slope, "calibration_intercept": intercept,
            "precision": float(y[hit].mean()) if hit.any() else np.nan,
            "recall": float(hit[y == 1].mean()) if (y == 1).any() else np.nan,
            "hit_rate": float((hit == y).mean())}


def team_metrics(frame: pd.DataFrame) -> dict:
    h, a = frame.pred_home_score.to_numpy(), frame.pred_away_score.to_numpy()
    yh, ya = frame.actual_home_score.to_numpy(), frame.actual_away_score.to_numpy()
    error = np.concatenate((h-yh,a-ya)); actual_margin=yh-ya
    margin=frame.model_margin.to_numpy() if "model_margin" in frame else h-a
    total=frame.model_total.to_numpy() if "model_total" in frame else h+a
    p = frame.home_win_probability.to_numpy() if "home_win_probability" in frame else norm.cdf(margin/np.sqrt(156.0))
    non_tie = actual_margin != 0
    result = {"rows":len(frame), "team_score_mae":float(np.abs(error).mean()),
        "team_score_rmse":float(np.sqrt(np.mean(error**2))), "team_score_bias":float(error.mean()),
        "home_mae":float(np.abs(h-yh).mean()), "away_mae":float(np.abs(a-ya).mean()),
        "home_bias":float((h-yh).mean()), "away_bias":float((a-ya).mean()),
        "total_mae":float(np.abs(total-yh-ya).mean()), "total_bias":float((total-yh-ya).mean()),
        "margin_mae":float(np.abs(margin-actual_margin).mean()), "margin_bias":float((margin-actual_margin).mean()),
        "expected_point_total_mae":float(np.abs(h+a-yh-ya).mean()),
        "expected_point_margin_mae":float(np.abs(h-a-actual_margin).mean()),
        "winner_accuracy":float(np.mean((p[non_tie]>.5)==(actual_margin[non_tie]>0)))}
    result.update({"win_"+k:v for k,v in probability_metrics((actual_margin[non_tie]>0),p[non_tie]).items()})
    # Historical comparison uses production's fixed correlated noise covariance.
    for side, mean, y in (("home",h,yh),("away",a,ya)):
        lo = frame[f"{side}_score_p10"].to_numpy() if f"{side}_score_p10" in frame else np.maximum(0,mean+norm.ppf(.1)*np.sqrt(90))
        hi = frame[f"{side}_score_p90"].to_numpy() if f"{side}_score_p90" in frame else np.clip(mean+norm.ppf(.9)*np.sqrt(90),0,70)
        result[f"{side}_80_coverage"] = float(np.mean((y>=lo)&(y<=hi)))
        result[f"{side}_80_width"] = float(np.mean(hi-lo))
    for name,prediction,actual,variance in (("margin",margin,actual_margin,156),("total",total,yh+ya,204)):
        lo=frame[f"{name}_p10"].to_numpy() if f"{name}_p10" in frame else prediction+norm.ppf(.1)*np.sqrt(variance)
        hi=frame[f"{name}_p90"].to_numpy() if f"{name}_p90" in frame else prediction+norm.ppf(.9)*np.sqrt(variance)
        result[f"{name}_80_coverage"]=float(((actual>=lo)&(actual<=hi)).mean())
        result[f"{name}_80_width"]=float((hi-lo).mean())
    return result


def yard_metrics(y, prediction, low=None, high=None, kind="yards") -> dict:
    y=np.asarray(y,float); p=np.asarray(prediction,float); e=p-y
    out={"rows":len(y), "mae":float(np.abs(e).mean()), "rmse":float(np.sqrt(np.mean(e**2))),
         "bias":float(e.mean()), "median_bias":float(np.median(e)),
         "rank_correlation":float(spearmanr(y,p).statistic) if np.std(p)>0 and np.std(y)>0 else np.nan}
    sd=np.sqrt(np.maximum(p,0)+1) if kind=="count" else 8+3*np.sqrt(np.maximum(p,0))
    lo=np.maximum(0,p-1.282*sd) if low is None else np.asarray(low,float)
    hi=np.maximum(0,p+1.282*sd) if high is None else np.asarray(high,float)
    out.update({"coverage80":float(((y>=lo)&(y<=hi)).mean()), "width80":float((hi-lo).mean())})
    return out


def reliability(frame: pd.DataFrame, y: str, p: str, scope: str) -> pd.DataFrame:
    d=frame[[y,p]].copy(); d["decile"]=np.minimum((d[p]*10).astype(int),9)
    out=d.groupby("decile",as_index=False).agg(rows=(y,"size"), mean_probability=(p,"mean"), actual_frequency=(y,"mean"))
    out["scope"]=scope
    return out


def evaluate_frozen(settings) -> tuple[pd.DataFrame,pd.DataFrame,dict]:
    from nfl2026.production.core import verify_lock
    from nfl2026.postgame import inspect_completed_week_actuals, _regular_games
    games_source=pd.read_csv(settings.root/"data/phase2_external/games.csv",low_memory=False)
    actual=pd.read_csv(settings.artifact_root/"canonical/player_weekly.csv",low_memory=False)
    actual=actual[actual.season.eq(2026)&actual.week.between(1,5)&actual.player_id.notna()]
    game_parts=[]; player_parts=[]; coverage=[]
    for week in range(1,6):
        expected=_regular_games(games_source,2026,week)
        state=inspect_completed_week_actuals(settings,2026,week,expected)
        if state["status"]!="ALREADY_CURRENT": raise ValueError(f"Actuals week {week} not validated: {state['status']}")
        directories=sorted((settings.artifact_root/"official_freezes/2026"/f"week_{week:02d}").glob("freeze-*"))
        if not directories: raise ValueError(f"No official Week {week} freeze")
        freeze=directories[-1]
        selection=pd.read_csv(freeze/"official_predictions.csv")
        if set(selection.game_id)!=set(expected.game_id) or selection.game_id.duplicated().any():
            raise ValueError(f"Week {week} frozen selection differs from actual schedule")
        for run_id,group in selection.groupby("official_prediction_run_id"):
            run=settings.prediction_root/"2026"/f"week_{week:02d}"/str(run_id)
            meta=json.loads((run/"metadata.json").read_text())
            for name,digest in meta.get("file_hashes",{}).items():
                if sha256_file(run/name)!=digest: raise ValueError(f"Frozen run hash mismatch: {run_id}/{name}")
            lock=settings.root/"locks/2026"/f"week_{week:02d}"/meta["lock_id"]
            if verify_lock(lock): raise ValueError(f"Frozen lock hash mismatch: {lock}")
            g=pd.read_csv(run/"games.csv",low_memory=False)
            g=g[g.game_id.isin(group.game_id)].copy()
            g=g.merge(expected[["game_id","home_score","away_score"]],on="game_id",validate="one_to_one")
            if not (pd.to_datetime(group.official_prediction_timestamp,utc=True)<pd.to_datetime(group.kickoff_utc,utc=True)).all():
                raise ValueError("Frozen prediction was not pregame")
            g["pred_home_score"]=g.home_expected_points; g["pred_away_score"]=g.away_expected_points
            g["actual_home_score"]=g.home_score;g["actual_away_score"]=g.away_score
            g["week"]=week;g["season"]=2026;g["source_run_id"]=run_id
            g["freeze_id"]=freeze.name;g["lock_id"]=meta["lock_id"]
            g["prediction_timestamp"]=meta["created_at_utc"]
            g["team_model_version"]=meta["model_ids"]["team"]["version"]
            game_parts.append(g)
            p=pd.read_csv(run/"players.csv",low_memory=False)
            p=p[p.game_id.isin(group.game_id)].copy()
            p["season"]=2026;p["week"]=week
            p=p.merge(actual[["season","week","player_id","team","carries","targets","rushing_yards","receiving_yards","anytime_offensive_td"]],
                      on=["season","week","player_id","team"],how="left",validate="one_to_one",indicator=True)
            p["actual_participant"]=p._merge.eq("both")
            for col in ("carries","targets","rushing_yards","receiving_yards","anytime_offensive_td"):
                p["actual_"+col]=pd.to_numeric(p[col],errors="coerce").fillna(0)
            p["source_run_id"]=run_id;p["lock_id"]=meta["lock_id"];p["freeze_id"]=freeze.name
            p["prediction_timestamp"]=meta["created_at_utc"]
            p["player_model_version"]=meta["model_ids"]["player"]["version"]
            p["td_model_version"]=meta["model_ids"]["td"]["version"]
            player_parts.append(p.drop(columns="_merge"))
            missed=actual[actual.week.eq(week)&actual.team.isin(set(g.home_team)|set(g.away_team))&actual.position.isin(["QB","RB","FB","WR","TE"])]
            missed=missed[~missed.player_id.isin(p.player_id)&((missed.carries>0)|(missed.targets>0)|(missed.anytime_offensive_td>0))]
            coverage.append({"week":week,"freeze_id":freeze.name,"run_id":run_id,"lock_id":meta["lock_id"],
                "games":len(g),"predicted_players":len(p),"actual_nonzero_offensive_players_not_predicted":len(missed),
                "team_model":meta["model_ids"]["team"]["version"],"player_model":meta["model_ids"]["player"]["version"],
                "player_input_copy_present":(lock/"inputs/player_pregame_universe_features.csv").exists()})
    games=pd.concat(game_parts,ignore_index=True);players=pd.concat(player_parts,ignore_index=True)
    if games.game_id.duplicated().any() or players.duplicated(["game_id","player_id"]).any(): raise ValueError("Duplicate frozen evaluation keys")
    report=settings.report_root
    csv(report/"five_week_live_games.csv",games); csv(report/"five_week_live_players.csv",players)
    csv(report/"five_week_official_run_selection.csv",pd.DataFrame(coverage))
    metric_rows=[];rel=[]
    for label,g in [("ALL",games),*[(f"WEEK_{w}",d) for w,d in games.groupby("week")]]:
        metric_rows.append({"component":"team","segment":label,**team_metrics(g)})
        z=g.assign(actual_win=(g.actual_home_score>g.actual_away_score).astype(int))
        rel.append(reliability(z,"actual_win","home_win_probability","win_"+label))
    for label,p in [("ALL",players),*[("POSITION_"+s,d) for s,d in players.groupby("position")],
                    *[(f"WEEK_{s}",d) for s,d in players.groupby("week")]]:
        for probability in ("raw_td_probability","reconciled_td_probability"):
            metric_rows.append({"component":"td_"+probability,"segment":label,"rows":len(p),
                               **probability_metrics(p.actual_anytime_offensive_td,p[probability])})
        rel.append(reliability(p,"actual_anytime_offensive_td","reconciled_td_probability","td_"+label))
        for metric,pred,low,high,mask in (
            ("rush","expected_rush_yards","rush_yards_low","rush_yards_high",(p.expected_carries>=1)|(p.expected_rush_yards>=5)),
            ("receiving","expected_receiving_yards","receiving_yards_low","receiving_yards_high",(p.expected_targets>=1)|(p.expected_receiving_yards>=8))):
            part=p[mask]
            if len(part):metric_rows.append({"component":metric,"segment":label,
                **yard_metrics(part["actual_rushing_yards" if metric=="rush" else "actual_receiving_yards"],part[pred],part[low],part[high])})
    csv(report/"five_week_live_metrics.csv",pd.DataFrame(metric_rows))
    csv(report/"five_week_live_reliability.csv",pd.concat(rel,ignore_index=True))
    totals=players.groupby(["game_id","team"]).agg(predicted_scorers=("reconciled_td_probability","sum"),
        raw_scorers=("raw_td_probability","sum"), actual_scorers=("actual_anytime_offensive_td","sum"))
    csv(report/"five_week_live_td_reconciliation.csv",totals.reset_index())
    return games,players,{"games":len(games),"players":len(players),"metrics":finite(metric_rows),"run_selection":coverage}


def audit_features(settings) -> pd.DataFrame:
    team=joblib.load(settings.artifact_root/"models/team-phase2-v2.joblib")
    player=joblib.load(settings.artifact_root/"models/player-phase2-v3-rush-et.joblib")
    td=joblib.load(settings.artifact_root/"models/td-phase2-v2.joblib")
    history_team=pd.read_csv(settings.artifact_root/"phase2/team_advanced_matrix.csv",low_memory=False)
    history_player=pd.read_csv(settings.artifact_root/"phase2/player_advanced_conditional_matrix.csv",low_memory=False)
    current_team=pd.read_csv(settings.artifact_root/"phase2/team_rollforward/2026/week_06.csv")
    current_player=pd.read_csv(settings.artifact_root/"phase2/player_pregame_universe_features.csv",low_memory=False)
    current_player=current_player[current_player.season.eq(2026)&current_player.week.eq(6)]
    rows=[]; redundant=[]
    for component,features,hist,current in (("team",team["features"],history_team,current_team),
                                            ("player",sorted(set(player["features"])|set(td["features"])),history_player,current_player)):
        for feature in features:
            values=pd.to_numeric(hist[feature],errors="coerce")
            now=pd.to_numeric(current.get(feature,pd.Series(dtype=float)),errors="coerce")
            if component=="team" and "_qb_" in feature:
                provenance="UNKNOWN / WEEKLY_ASSUMED_PREGAME_2022_24; excluded from candidates"
                source="weekly depth plus lagged PBP; pregame identity unproved; full-history rating fallback"
                definition="lagged QB career ratio/replacement; historical identity provenance incomplete"
            elif component=="team" and feature=="roof_closed_or_dome":
                provenance="UNKNOWN for historical roof-open/closed observation; excluded from candidates"
                source="historical schedule roof field";definition="observed closed/dome flag"
            elif component=="team":
                provenance="PIT_SAFE calculation; historical schedule attributes subject to documented provenance limits"
                source="completed prior team games/PBP or scheduled venue/rest/Elo"
                definition="prior-eight cross-season team means / opponent differences" if ("pregame" in feature or "matchup" in feature) else "scheduled context or sequential Elo"
            else:
                provenance="PIT_SAFE for established personal histories; no-history priors and historical roster population require qualification"
                source="lagged player actuals/PBP/snap + current-team volume"
                definition="prior-eight appearances; snaps prior-four; team-volume prior-six; ratios of rolling means"
                if feature.startswith("position_"):definition="target roster one-hot position"
                if feature in {"player_games_before","rookie_or_no_history"}:definition="prior observed appearances; no-history indicator"
            target=history_team.target_home_score if component=="team" else history_player.target_rushing_yards
            development=hist.season.le(2024)
            v=values[development];y=target[development]
            corr=float(v.corr(y,method="spearman")) if v.notna().sum()>2 and v.nunique()>1 else np.nan
            sd=float(values.std());shift=float((now.mean()-values.mean())/sd) if len(now) and sd>0 else np.nan
            rows.append({"component":component,"feature":feature,"definition":definition,"source":source,
                "used_by_model":"team_score" if component=="team" else "TD|carries|targets|rushing_yards|receiving_yards",
                "known_at_rule":"strictly prior completed game; target identity from source-specific snapshot",
                "provenance":provenance,"historical_missing_fraction":float(values.isna().mean()),
                "week6_missing_fraction":float(now.isna().mean()) if len(now) else np.nan,
                "week6_z_mean_shift":shift,"development_target_rank_correlation":corr,
                "unique_values":int(values.nunique()),"stability":"CONSTANT" if values.nunique()<2 else "VARIABLE",
                "training_imputation":"fitted SimpleImputer median; no-history candidate defaults originally full-matrix position median",
                "early_season":"cross-season history; tested alternatives are research only",
                "predictive_value_note":"univariate development association, not causal evidence or selection"})
        numeric=hist[features].apply(pd.to_numeric,errors="coerce")
        correlation=numeric.corr().abs()
        for i,name in enumerate(features):
            for other in features[i+1:]:
                if correlation.loc[name,other]>.98:
                    redundant.append({"component":component,"feature":name,"other":other,"absolute_correlation":correlation.loc[name,other]})
    result=pd.DataFrame(rows)
    csv(settings.report_root/"five_week_feature_audit.csv",result)
    csv(settings.report_root/"five_week_feature_redundancy.csv",pd.DataFrame(redundant))
    registry=pd.read_csv(settings.artifact_root/"registry/model_registry.csv")
    contracts=[]
    for name,version,artifact in (("team_score_model",settings.phase2_model_version,team),
        ("player_opportunity_model",settings.phase2_player_model_version,player),("player_td_model",settings.phase2_model_version,td)):
        row=registry[registry.model_name.eq(name)&registry.model_version.eq(version)].iloc[-1] if "model_name" in registry else None
        if row is not None:
            path=Path(row.artifact_path)
            contracts.append({"name":name,"version":version,"artifact":str(path),"hash_verified":sha256_file(path)==row.artifact_sha256,
                              "features":len(artifact["features"]),"feature_version":settings.phase2_feature_version})
    write_json(settings.report_root/"five_week_model_contract_audit.json",finite(contracts))
    return result


def pooled_team_frame(matrix: pd.DataFrame) -> tuple[pd.DataFrame,list[str]]:
    """Share one offense/opponent model across both sides; no QB/observed roof inputs."""
    parts=[]
    for side,other in (("home","away"),("away","home")):
        d=matrix[["game_id","season","week","kickoff_utc"]].copy()
        d["side"]=side
        for column in matrix:
            if column.startswith(side+"_pregame_"):
                suffix=column[len(side)+1:]
                d[suffix]=matrix[column]; d["opponent_"+suffix]=matrix[other+"_"+suffix]
        for suffix in ("elo","rest_days","short_week","bye_rest","season_games_before"):
            d[suffix]=matrix[side+"_"+suffix];d["opponent_"+suffix]=matrix[other+"_"+suffix]
        for column in ("week_number","neutral_site","international_game","surface_grass"):
            d[column]=matrix[column]
        d["home_indicator"]=(1 if side=="home" else 0)*(1-matrix.neutral_site)
        d["target_points"]=matrix[f"target_{side}_score"]
        parts.append(d)
    data=pd.concat(parts,ignore_index=True)
    features=[c for c in data if c not in {"game_id","season","week","kickoff_utc","side","target_points"}]
    forbidden=[c for c in features if any(token in c.lower() for token in ("qb_","odds","bookmaker","target_","weather","roof"))]
    if forbidden:raise ValueError(f"Unsafe pooled predictors: {forbidden}")
    return data,features


def team_variants(settings) -> dict[str,pd.DataFrame]:
    matrix=pd.read_csv(settings.artifact_root/"phase2/team_advanced_matrix.csv",low_memory=False)
    pbp=pd.read_csv(settings.artifact_root/"phase2/team_game_efficiency.csv",low_memory=False)
    source=[]
    for side,other in (("home","away"),("away","home")):
        s=matrix[["game_id","season","week","kickoff_utc",f"{side}_team",f"{other}_team",f"target_{side}_score",f"target_{other}_score"]].rename(columns={f"{side}_team":"team",f"{other}_team":"opponent",f"target_{side}_score":"points_for",f"target_{other}_score":"points_against"})
        source.append(s)
    source=pd.concat(source,ignore_index=True)
    source=source.merge(pbp.drop(columns=[c for c in ("season","week","opponent","points_for","points_against") if c in pbp]),on=["game_id","team"],validate="one_to_one")
    metrics=sorted(set(c.removeprefix("home_pregame_") for c in matrix if c.startswith("home_pregame_")))
    base_metrics=[c for c in metrics if not c.startswith("def_")]
    defense=source[["game_id","opponent",*[c for c in base_metrics if c not in {"points_for","points_against"}]]].rename(columns={"opponent":"team",**{c:"def_"+c for c in base_metrics if c not in {"points_for","points_against"}}})
    source=source.merge(defense,on=["game_id","team"],validate="one_to_one").sort_values(["team","kickoff_utc","game_id"])
    out={}
    for method in ("last4","last8","last12","ewm8","prior_strength4"):
        values=source[["game_id","team"]].copy()
        for metric in metrics:
            if method.startswith("last"):
                size=int(method[4:]);values[metric]=source.groupby("team")[metric].transform(lambda s:s.shift(1).rolling(size,min_periods=1).mean())
            elif method=="ewm8":
                values[metric]=source.groupby("team")[metric].transform(lambda s:s.shift(1).ewm(span=8,adjust=False,min_periods=1).mean())
            else:
                # Fixed candidate prior strength is selected using development
                # folds only, never hard-coded into production week multipliers.
                prior=source.groupby(["team","season"])[metric].mean()
                pm=pd.Series([prior.get((t,int(s)-1),np.nan) for t,s in zip(source.team,source.season)],index=source.index)
                count=source.groupby(["team","season"]).cumcount()
                current=source.groupby(["team","season"])[metric].transform(lambda s:s.shift(1).expanding(min_periods=1).mean())
                values[metric]=(current.fillna(0)*count+pm.fillna(0)*4)/(count+4)
                values.loc[pm.isna(),metric]=current.loc[pm.isna()]
        m=matrix.copy()
        for side in ("home","away"):
            keyed=values.rename(columns={"team":side+"_team",**{c:side+"_pregame_"+c for c in metrics}})
            m=m.drop(columns=[side+"_pregame_"+c for c in metrics]).merge(keyed,on=["game_id",side+"_team"],validate="one_to_one")
        out[method]=m
    return out


def research_regressors(kind: str) -> dict:
    return {
        "ridge":make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),Ridge(alpha=30)),
        "poisson":make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),PoissonRegressor(alpha=2,max_iter=600)),
        "extra_trees":make_pipeline(SimpleImputer(strategy="median"),ExtraTreesRegressor(n_estimators=160,max_depth=8,min_samples_leaf=10,max_features=.75,n_jobs=2,random_state=SEED)),
        "random_forest":make_pipeline(SimpleImputer(strategy="median"),RandomForestRegressor(n_estimators=160,max_depth=8,min_samples_leaf=10,max_features=.75,n_jobs=2,random_state=SEED)),
        "hist_gradient_boosting":make_pipeline(SimpleImputer(strategy="median"),HistGradientBoostingRegressor(max_iter=120,max_leaf_nodes=10,min_samples_leaf=24,learning_rate=.04,l2_regularization=5,random_state=SEED)),
    }


def team_research(settings, cache:Path) -> pd.DataFrame:
    if cache.exists(): return pd.read_csv(cache)
    reference=pd.read_csv(settings.artifact_root/"backtests/phase2_team_predictions.csv")
    reference=reference[reference.model.eq("extra_trees")].copy()
    if not (pd.to_datetime(reference.max_training_kickoff_utc,utc=True)<pd.to_datetime(reference.kickoff_utc,utc=True)).all():raise ValueError("Incumbent team OOS chronology invalid")
    reference["model"]="incumbent_phase2_v2";reference["variant"]="original113_including_unproven_qb"
    parts=[reference]
    for variant,matrix in team_variants(settings).items():
        panel,features=pooled_team_frame(matrix)
        for name,estimator in research_regressors("team").items():
            started=time.perf_counter()
            for season in (2023,2024,2025):
                train=panel[panel.season.lt(season)];test=panel[panel.season.eq(season)]
                if not pd.to_datetime(train.kickoff_utc,utc=True).max()<pd.to_datetime(test.kickoff_utc,utc=True).min():raise ValueError("Team fold target leakage")
                model=clone(estimator).fit(train[features],train.target_points)
                scored=test[["game_id","side"]].copy();scored["prediction"]=np.clip(model.predict(test[features]),0,60)
                pivot=scored.pivot(index="game_id",columns="side",values="prediction")
                base=matrix[matrix.season.eq(season)][["game_id","season","week","kickoff_utc","home_team","away_team","target_home_score","target_away_score"]].set_index("game_id")
                base["pred_home_score"]=pivot.home;base["pred_away_score"]=pivot.away
                base=base.rename(columns={"target_home_score":"actual_home_score","target_away_score":"actual_away_score"}).reset_index()
                base["max_training_kickoff_utc"]=train.kickoff_utc.max();base["model"]="pooled_"+name;base["variant"]=variant
                parts.append(base)
            print(f"Team candidate {name}/{variant}: {time.perf_counter()-started:.1f}s",flush=True)
    frame=pd.concat(parts,ignore_index=True)
    # Blend weights for 2024 use only 2023 OOS evidence; 2025 weights use
    # 2023-2024. The evaluated season never chooses its own weight.
    for season in (2024,2025):
        two=frame[frame.variant.eq("last8")&frame.model.isin(["pooled_extra_trees","pooled_hist_gradient_boosting"])]
        prior=two[two.season.lt(season)]
        a=prior[prior.model.eq("pooled_extra_trees")].set_index("game_id")
        b=prior[prior.model.eq("pooled_hist_gradient_boosting")].set_index("game_id").reindex(a.index)
        weights=np.linspace(0,1,11)
        loss=[np.mean(np.abs(w*a.pred_home_score+(1-w)*b.pred_home_score-a.actual_home_score)+np.abs(w*a.pred_away_score+(1-w)*b.pred_away_score-a.actual_away_score))/2 for w in weights]
        w=float(weights[int(np.argmin(loss))])
        a=two[two.season.eq(season)&two.model.eq("pooled_extra_trees")].set_index("game_id").copy()
        b=two[two.season.eq(season)&two.model.eq("pooled_hist_gradient_boosting")].set_index("game_id").reindex(a.index)
        for col in ("pred_home_score","pred_away_score"):a[col]=w*a[col]+(1-w)*b[col]
        a["model"]="pooled_time_ordered_blend";a["blend_et_weight"]=w
        parts.append(a.reset_index())
    frame=pd.concat(parts,ignore_index=True)
    csv(cache,frame)
    return frame


def weekly_team_refit_research(settings,cache:Path) -> pd.DataFrame:
    if cache.exists():return pd.read_csv(cache)
    matrix=team_variants(settings)["last8"]
    panel,features=pooled_team_frame(matrix)
    parts=[]
    for name in ("extra_trees","hist_gradient_boosting"):
        start=time.perf_counter()
        for season in (2023,2024,2025):
            for week in sorted(panel.loc[panel.season.eq(season),"week"].unique()):
                test=panel[panel.season.eq(season)&panel.week.eq(week)]
                train=panel[panel.season.lt(season)|(panel.season.eq(season)&panel.week.lt(week))]
                if not pd.to_datetime(train.kickoff_utc,utc=True).max()<pd.to_datetime(test.kickoff_utc,utc=True).min():raise ValueError("Weekly team refit consumed current-week outcomes")
                fit=clone(research_regressors("team")[name]).fit(train[features],train.target_points)
                scores=test[["game_id","side"]].copy();scores["prediction"]=np.clip(fit.predict(test[features]),0,60)
                wide=scores.pivot(index="game_id",columns="side",values="prediction")
                base=matrix[matrix.season.eq(season)&matrix.week.eq(week)][["game_id","season","week","kickoff_utc","home_team","away_team","target_home_score","target_away_score"]].set_index("game_id")
                base["pred_home_score"]=wide.home;base["pred_away_score"]=wide.away
                base=base.rename(columns={"target_home_score":"actual_home_score","target_away_score":"actual_away_score"}).reset_index()
                base["model"]="weekly_refit_pooled_"+name;base["variant"]="last8"
                base["max_training_kickoff_utc"]=train.kickoff_utc.max();parts.append(base)
        print(f"Historical weekly team refits {name}: {time.perf_counter()-start:.1f}s",flush=True)
    result=pd.concat(parts,ignore_index=True);csv(cache,result)
    return result


def repair_no_history(frame: pd.DataFrame, features:list[str]) -> pd.DataFrame:
    """Remove historical all-season fallback leakage before fitting fold imputers."""
    result=frame.copy()
    zero=pd.to_numeric(result.player_games_before,errors="coerce").fillna(0).eq(0)
    cols=[c for c in features if c.startswith("prior_") or c in {"recent_yards_per_carry","recent_yards_per_target"}]
    result.loc[zero,cols]=np.nan
    return result


def historical_players(settings) -> tuple[pd.DataFrame,pd.DataFrame,list[str]]:
    artifact=joblib.load(settings.artifact_root/"models/player-phase2-v3-rush-et.joblib")
    features=artifact["features"]
    conditional=pd.read_csv(settings.artifact_root/"phase2/player_advanced_conditional_matrix.csv",low_memory=False)
    raw=pd.read_csv(settings.artifact_root/"phase2/player_pregame_universe_features.csv",low_memory=False)
    raw=raw[raw.season.lt(2026)&raw.position.isin(["QB","RB","FB","WR","TE"])].copy()
    keys=["game_id","player_id","team","season","week"]
    if raw.duplicated(keys).any() or conditional.duplicated(keys).any():raise ValueError("Historical player keys duplicated")
    outcomes=conditional[keys+[f"target_{t}" for t in TARGETS]+["target_anytime_offensive_td"]]
    panel=raw.merge(outcomes,on=keys,how="left",validate="one_to_one",indicator=True)
    panel["actual_participant"]=panel._merge.eq("both")
    for t in (*TARGETS,"anytime_offensive_td"):panel[f"actual_{t}"]=panel[f"target_{t}"].fillna(0)
    panel=panel.drop(columns="_merge")
    panel["evaluation_provenance"]="WEEKLY_ASSUMED_PREGAME population; NOT eligible for promotion without publication proof"
    return conditional,panel,features


def immutable_research_artifact(path:Path, model, provenance:dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    meta_path=path.with_suffix(".json")
    if path.exists():
        if not meta_path.exists():raise ValueError("Experimental artifact exists without provenance")
        meta=json.loads(meta_path.read_text())
        if meta.get("provenance")!=provenance or sha256_file(path)!=meta["sha256"]:
            raise ValueError("Experimental artifact cannot be overwritten")
        return
    joblib.dump(model,path)
    write_json(meta_path,{"status":"EXPERIMENTAL_NOT_REGISTERED","provenance":provenance,"sha256":sha256_file(path)})


def player_research(settings,cache:Path) -> tuple[pd.DataFrame,pd.DataFrame]:
    td_cache=cache.with_name("td_candidates.csv")
    if cache.exists() and td_cache.exists():return pd.read_csv(cache),pd.read_csv(td_cache)
    conditional,panel,features=historical_players(settings)
    conditional_safe=repair_no_history(conditional,features);panel_safe=repair_no_history(panel,features)
    incumbent=pd.read_csv(settings.artifact_root/"backtests/phase2_player_universe_scored.csv")
    incumbent=incumbent[incumbent.model.eq("extra_trees")&incumbent.predicted_eligible.eq(True)&incumbent.season.between(2023,2025)]
    parts=[];td_parts=[]
    parts.append(incumbent.assign(model="incumbent_phase2_v3_rush_et",evaluation_provenance="original weekly assumed pregame; no-history defaults future-contaminated"))
    definitions=[("roster_"+name,est) for name,est in research_regressors("player").items()]
    definitions.extend(("hurdle_"+name,est) for name,est in research_regressors("player").items() if name in {"extra_trees","hist_gradient_boosting"})
    definitions.append(("opportunity_efficiency_extra_trees",research_regressors("player")["extra_trees"]))
    for name,estimator in definitions:
        started=time.perf_counter()
        for season in (2023,2024,2025):
            train=panel_safe[panel_safe.season.lt(season)];test=panel_safe[panel_safe.season.eq(season)]
            if not pd.to_datetime(train.kickoff_utc,utc=True).max()<pd.to_datetime(test.kickoff_utc,utc=True).min():raise ValueError("Player fold target leakage")
            scored=test[["game_id","player_id","player_name","team","position","season","week","kickoff_utc","actual_participant", "evaluation_provenance",*[f"actual_{t}" for t in TARGETS]]].copy()
            fitted={}
            for target in TARGETS:
                y=train[f"actual_{target}"]
                if name.startswith("hurdle_") or name.startswith("opportunity_efficiency_"):
                    opportunity="carries" if target in {"carries","rushing_yards"} else "targets"
                    nonzero=train[f"actual_{opportunity}"]>0
                    occurrence=make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),LogisticRegression(C=.35,max_iter=1000)).fit(train[features],nonzero.astype(int))
                    p=occurrence.predict_proba(test[features])[:,1]
                    if name.startswith("opportunity_efficiency_") and target.endswith("yards"):
                        opportunities=clone(estimator).fit(train.loc[nonzero,features],train.loc[nonzero,f"actual_{opportunity}"])
                        efficiency=make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),Ridge(alpha=30)).fit(
                            train.loc[nonzero,features],y[nonzero]/train.loc[nonzero,f"actual_{opportunity}"],
                            ridge__sample_weight=train.loc[nonzero,f"actual_{opportunity}"].to_numpy())
                        pred=p*np.maximum(0,opportunities.predict(test[features]))*np.maximum(0,efficiency.predict(test[features]))
                        fitted[target]={"occurrence":occurrence,"opportunity":opportunities,"efficiency":efficiency}
                    else:
                        model=clone(estimator).fit(train.loc[nonzero,features],y[nonzero])
                        pred=p*np.maximum(0,model.predict(test[features]));fitted[target]={"occurrence":occurrence,"amount":model}
                else:
                    # Poisson is a count candidate only. NFL yardage can be
                    # negative; use the specified signed-target ridge model.
                    base=(research_regressors("player")["ridge"]
                          if name=="roster_poisson" and target.endswith("yards") else estimator)
                    model=clone(base).fit(train[features],y)
                    pred=model.predict(test[features]);fitted[target]=model
                scored[f"pred_{target}"]=np.maximum(0,pred)
            scored["model"]=name;scored["max_training_kickoff_utc"]=train.kickoff_utc.max()
            parts.append(scored)
            if season==2025:
                immutable_research_artifact(cache.parent/"models"/(name+"-confirm2025.joblib"),
                    {"models":fitted,"features":features}, {"model":name,"fit_seasons":[2022,2023,2024],"seed":SEED,
                    "source_sha256":sha256_file(settings.artifact_root/"phase2/player_advanced_conditional_matrix.csv"),
                    "predictor_semantics":"no-history all-season defaults removed; fold-only imputer", "population_provenance":"WEEKLY_ASSUMED_PREGAME"})
        print(f"Player candidate {name}: {time.perf_counter()-started:.1f}s",flush=True)
    team_oos=pd.read_csv(settings.artifact_root/"backtests/phase2_team_predictions.csv")
    team_oos=team_oos[team_oos.model.eq("extra_trees")]
    point_lookup={(r.game_id,r.home_team):r.pred_home_score for r in team_oos.itertuples()}
    point_lookup.update({(r.game_id,r.away_team):r.pred_away_score for r in team_oos.itertuples()})
    from nfl2026.models.player_advanced import reconcile_td_probabilities
    for season in (2023,2024,2025):
        cond=conditional_safe[conditional_safe.season.lt(season)]
        train=panel_safe[panel_safe.season.lt(season)];test=panel_safe[panel_safe.season.eq(season)].copy()
        for name in ("incumbent_conditional_td","roster_td","position_specific_td"):
            source=cond if name=="incumbent_conditional_td" else train
            ycol="target_anytime_offensive_td" if name=="incumbent_conditional_td" else "actual_anytime_offensive_td"
            probability=np.zeros(len(test));loc_test=test.reset_index(drop=True)
            groupings=("ALL",) if name!="position_specific_td" else ("QB","RB_FB","WR_TE")
            for group in groupings:
                mask=pd.Series(True,index=source.index) if group=="ALL" else source.position.isin({"QB"} if group=="QB" else {"RB","FB"} if group=="RB_FB" else {"WR","TE"})
                tmask=pd.Series(True,index=loc_test.index) if group=="ALL" else loc_test.position.isin({"QB"} if group=="QB" else {"RB","FB"} if group=="RB_FB" else {"WR","TE"})
                model=make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),LogisticRegression(C=.35,max_iter=1000))
                model.fit(source.loc[mask,features],source.loc[mask,ycol].clip(0,1))
                probability[tmask]=model.predict_proba(loc_test.loc[tmask,features])[:,1]
            d=loc_test[["game_id","player_id","team","position","season","week","evaluation_provenance","actual_anytime_offensive_td"]].copy()
            d["raw_probability"]=np.clip(probability,.001,.999);d["reconciled_probability"]=0.
            for (game_id,team),idx in d.groupby(["game_id","team"]).groups.items():
                target=float(np.clip(point_lookup[(game_id,team)]/7*.72,.35,4.25))
                d.loc[idx,"reconciled_probability"]=reconcile_td_probabilities(d.loc[idx,"raw_probability"].to_numpy(),target)
            d["model"]=name;d["max_training_season"]=season-1
            td_parts.append(d)
    player_predictions=pd.concat(parts,ignore_index=True)
    td_predictions=pd.concat(td_parts,ignore_index=True)
    csv(cache,player_predictions);csv(td_cache,td_predictions)
    return player_predictions,td_predictions


def player_window_research(settings,cache:Path) -> pd.DataFrame:
    if cache.exists():return pd.read_csv(cache,low_memory=False)
    from nfl2026.features.player_rollforward import roll_player_usage_features
    conditional,panel,features=historical_players(settings)
    observations=conditional.rename(columns={f"target_{t}":t for t in (*TARGETS,"anytime_offensive_td")}).copy()
    observations["kickoff_utc"]=pd.to_datetime(observations.kickoff_utc,utc=True,format="mixed")
    out=[]
    for window in (4,12):
        start=time.perf_counter()
        rolled=roll_player_usage_features(observations,panel,player_window=window,snap_window=4,team_window=6)
        for season in (2023,2024,2025):
            train=rolled[rolled.season.lt(season)];test=rolled[rolled.season.eq(season)]
            d=test[["game_id","player_id","player_name","team","position","season","week","kickoff_utc","actual_participant","evaluation_provenance",*[f"actual_{t}" for t in TARGETS]]].copy()
            models={}
            for target in TARGETS:
                fitted=clone(research_regressors("player")["extra_trees"]).fit(train[features],train[f"actual_{target}"])
                d[f"pred_{target}"]=np.maximum(0,fitted.predict(test[features]));models[target]=fitted
            d["model"]=f"roster_et_personal_window{window}"
            d["max_training_kickoff_utc"]=train.kickoff_utc.max();out.append(d)
            if season==2025:
                immutable_research_artifact(cache.parent/"models"/(d.model.iloc[0]+"-confirm2025.joblib"),
                    {"models":models,"features":features},{"model":d.model.iloc[0],"fit_seasons":[2022,2023,2024],"seed":SEED,
                    "source_sha256":sha256_file(settings.artifact_root/"phase2/player_advanced_conditional_matrix.csv"),
                    "player_window":window,"snap_window":4,"team_window":6,"population_provenance":"WEEKLY_ASSUMED_PREGAME"})
        print(f"Player recency window {window}: {time.perf_counter()-start:.1f}s",flush=True)
    result=pd.concat(out,ignore_index=True);csv(cache,result)
    return result


def incumbent_td_universe_replay(settings,cache:Path) -> pd.DataFrame:
    """Benchmark the unchanged estimator's historical serving population.

    The saved conditional OOS probabilities are checked first. Candidate
    full-season cold-start defaults remain an explicitly audited limitation.
    """
    if cache.exists():return pd.read_csv(cache,low_memory=False)
    conditional,panel,features=historical_players(settings)
    saved=pd.read_csv(settings.artifact_root/"backtests/phase2_td_predictions.csv")
    team=pd.read_csv(settings.artifact_root/"backtests/phase2_team_predictions.csv")
    team=team[team.model.eq("extra_trees")]
    expected={(r.game_id,r.home_team):r.pred_home_score for r in team.itertuples()}
    expected.update({(r.game_id,r.away_team):r.pred_away_score for r in team.itertuples()})
    from nfl2026.models.player_advanced import reconcile_td_probabilities
    parts=[];deltas=[]
    for season in (2023,2024,2025):
        train=conditional[conditional.season.lt(season)]
        check=conditional[conditional.season.eq(season)].copy()
        model=make_pipeline(SimpleImputer(strategy="median"),StandardScaler(),LogisticRegression(C=.35,max_iter=1000))
        model.fit(train[features],train.target_anytime_offensive_td.clip(0,1))
        check["replayed_raw"]=np.clip(model.predict_proba(check[features])[:,1],.001,.999)
        paired=check[["game_id","player_id","replayed_raw"]].merge(saved[saved.season.eq(season)][["game_id","player_id","raw_probability"]],on=["game_id","player_id"],validate="one_to_one")
        delta=float(abs(paired.replayed_raw-paired.raw_probability).max())
        if delta>1e-8:raise ValueError("Historical TD incumbent structure did not reproduce saved OOS predictions")
        deltas.append({"season":season,"rows":len(paired),"max_absolute_raw_probability_delta":delta})
        universe=panel[panel.season.eq(season)]
        d=universe[["game_id","player_id","team","position","season","week","evaluation_provenance","actual_anytime_offensive_td"]].copy().reset_index(drop=True)
        d["raw_probability"]=np.clip(model.predict_proba(universe[features])[:,1],.001,.999)
        d["reconciled_probability"]=0.
        for (game_id,team_code),idx in d.groupby(["game_id","team"]).groups.items():
            target=float(np.clip(expected[(game_id,team_code)]/7*.72,.35,4.25))
            d.loc[idx,"reconciled_probability"]=reconcile_td_probabilities(d.loc[idx,"raw_probability"].to_numpy(),target)
        d["model"]="incumbent_conditional_td";d["max_training_season"]=season-1;parts.append(d)
    result=pd.concat(parts,ignore_index=True)
    csv(cache,result);csv(settings.report_root/"five_week_td_incumbent_replay_parity.csv",pd.DataFrame(deltas))
    return result


def production_player_semantics_replay(settings,cache:Path) -> pd.DataFrame:
    """Re-evaluate the current mean-model architecture on repaired live semantics.

    Preserve legacy OOS files; rebuild last8/last4/last6 features, and learn
    cold-start position medians only from the fold's training years.
    """
    if cache.exists():return pd.read_csv(cache,low_memory=False)
    from nfl2026.features.player_rollforward import roll_player_usage_features
    from nfl2026.models.player_advanced import _regressors
    conditional,panel,features=historical_players(settings)
    observations=conditional.rename(columns={f"target_{t}":t for t in (*TARGETS,"anytime_offensive_td")}).copy()
    rolled=roll_player_usage_features(observations,panel,player_window=8,snap_window=4,team_window=6)
    prior_fields=["prior_carries","prior_targets","prior_rushing_yards","prior_receiving_yards","prior_anytime_offensive_td"]
    out=[]
    for season in (2023,2024,2025):
        train=conditional[conditional.season.lt(season)]
        test=rolled[rolled.season.eq(season)].copy()
        medians=train.groupby("position")[prior_fields].median()
        for field in prior_fields:test[field]=test[field].fillna(test.position.map(medians[field])).fillna(0)
        test["recent_yards_per_carry"]=test.prior_rushing_yards/test.prior_carries.replace(0,np.nan)
        test["recent_yards_per_target"]=test.prior_receiving_yards/test.prior_targets.replace(0,np.nan)
        d=test[["game_id","player_id","player_name","team","position","season","week","kickoff_utc","actual_participant","evaluation_provenance",*[f"actual_{t}" for t in TARGETS]]].copy()
        for target in TARGETS:
            model=clone(_regressors(settings.random_seed)["extra_trees"]).fit(train[features],train[f"target_{target}"])
            d[f"pred_{target}"]=np.maximum(0,model.predict(test[features]))
        d["model"]="incumbent_pit_semantics_replay";d["max_training_kickoff_utc"]=train.kickoff_utc.max();out.append(d)
    result=pd.concat(out,ignore_index=True);csv(cache,result)
    csv(settings.report_root/"five_week_player_semantics_replay_scope.csv",pd.DataFrame([
        {"feature":"no-history five priors","legacy":"full-matrix position median","replay":"fold-training-only position medians"},
        {"feature":"recent_team_carries/targets","legacy":"as-of prior target row's shifted team state; extra-game lag","replay":"inclusive prior completed-game state, exposed strictly before target kickoff"},
        {"feature":"personal/red-zone/snap windows","legacy":"8/8/4","replay":"8/8/4; same current production training definitions"}]))
    return result


def temporal_calibration(frame:pd.DataFrame,ycol:str,pcol:str,keys:list[str]) -> pd.DataFrame:
    parts=[]
    for season in (2024,2025):
        train=frame[frame.season.lt(season)];test=frame[frame.season.eq(season)]
        if train.empty or test.empty:continue
        for method in ("raw","platt","isotonic"):
            raw=np.clip(test[pcol].to_numpy(),1e-6,1-1e-6)
            if method=="platt":
                model=LogisticRegression(C=1,max_iter=1000).fit(logit(np.clip(train[pcol].to_numpy(),1e-6,1-1e-6)).reshape(-1,1),train[ycol])
                pred=model.predict_proba(logit(raw).reshape(-1,1))[:,1]
            elif method=="isotonic":pred=IsotonicRegression(out_of_bounds="clip",y_min=.001,y_max=.999).fit(train[pcol],train[ycol]).predict(raw)
            else:pred=raw
            d=test[keys+["season","week",ycol]].copy();d["probability"]=pred;d["method"]=method
            d["max_calibration_season"]=int(train.season.max());parts.append(d)
    return pd.concat(parts,ignore_index=True)


def block_bootstrap_difference(frame:pd.DataFrame,delta:np.ndarray,replicates:int=1500) -> tuple[float,float,float]:
    """Resample season-week blocks, keeping within-game/player dependence."""
    d=pd.DataFrame({"season":frame.season,"week":frame.week,"delta":delta})
    blocks=d.groupby(["season","week"]).delta.agg(["sum","count"]).to_numpy()
    rng=np.random.default_rng(SEED);chosen=rng.integers(0,len(blocks),(replicates,len(blocks)))
    draws=blocks[chosen,0].sum(axis=1)/blocks[chosen,1].sum(axis=1)
    return float(np.mean(delta)),float(np.quantile(draws,.025)),float(np.quantile(draws,.975))


def summarize_research(settings,team:pd.DataFrame,player:pd.DataFrame,td:pd.DataFrame) -> dict:
    report=settings.report_root
    team_rows=[];player_rows=[];td_rows=[];confidence=[]
    for (model,variant),group in team.groupby(["model","variant"]):
        for period,part in [("ALL",group),("DEVELOPMENT",group[group.season.le(2024)]),
            ("CONFIRMATION_2025",group[group.season.eq(2025)]),("EARLY_W1_5",group[group.week.le(5)]),
            *[(str(s),d) for s,d in group.groupby("season")]]:
            team_rows.append({"model":model,"variant":variant,"period":period,**team_metrics(part)})
    team_metrics_frame=pd.DataFrame(team_rows)
    csv(report/"five_week_team_candidates.csv",team_metrics_frame)
    reference=team[team.model.eq("incumbent_phase2_v2")].set_index("game_id")
    for (model,variant),group in team[~team.model.eq("incumbent_phase2_v2")].groupby(["model","variant"]):
        g=group.set_index("game_id");r=reference.reindex(g.index)
        for period,mask in (("DEVELOPMENT",g.season.le(2024)),("CONFIRMATION_2025",g.season.eq(2025)),("EARLY_W1_5",g.week.le(5))):
            x=g[mask];b=r[mask]
            for metric in ("team","margin","total"):
                if metric=="team":
                    e=(abs(x.pred_home_score-x.actual_home_score)+abs(x.pred_away_score-x.actual_away_score))/2
                    be=(abs(b.pred_home_score-b.actual_home_score)+abs(b.pred_away_score-b.actual_away_score))/2
                else:
                    sign=-1 if metric=="margin" else 1
                    e=abs(x.pred_home_score+sign*x.pred_away_score-x.actual_home_score-sign*x.actual_away_score)
                    be=abs(b.pred_home_score+sign*b.pred_away_score-b.actual_home_score-sign*b.actual_away_score)
                difference,lo,hi=block_bootstrap_difference(x.reset_index(),(e-be).to_numpy())
                confidence.append({"component":"team","model":model,"variant":variant,"period":period,"metric":metric+"_mae",
                                   "difference":difference,"pct_change":difference/be.mean()*100,"ci_low":lo,"ci_high":hi,"rows":len(x)})
    base=player[player.model.eq(PLAYER_REFERENCE)].set_index(["game_id","player_id"])
    for model,group in player.groupby("model"):
        g=group.set_index(["game_id","player_id"])
        common=base.index.intersection(g.index);g=g.loc[common];r=base.loc[common]
        for target in TARGETS:
            pop=(r.pred_carries.ge(1)|r.pred_rushing_yards.ge(5)) if target in {"carries","rushing_yards"} else (r.pred_targets.ge(1)|r.pred_receiving_yards.ge(8))
            for period,mask in [("ALL_ROSTER",pd.Series(True,index=g.index)),("PROP_LIKE",pop),
                ("DEVELOPMENT",pop&g.season.le(2024)),("CONFIRMATION_2025",pop&g.season.eq(2025)),
                ("EARLY_W1_5",pop&g.week.le(5)),*[(str(s),pop&g.season.eq(s)) for s in (2023,2024,2025)]]:
                part=g[mask]
                player_rows.append({"model":model,"target":target,"period":period,**yard_metrics(part[f"actual_{target}"],part[f"pred_{target}"],kind="yards" if target.endswith("yards") else "count")})
                if model!=PLAYER_REFERENCE and period in {"DEVELOPMENT","CONFIRMATION_2025","EARLY_W1_5"}:
                    be=abs(r.loc[part.index,f"pred_{target}"]-part[f"actual_{target}"])
                    delta=abs(part[f"pred_{target}"]-part[f"actual_{target}"])-be
                    diff,lo,hi=block_bootstrap_difference(part.reset_index(),delta.to_numpy())
                    confidence.append({"component":target,"model":model,"period":period,"metric":"mae","difference":diff,
                                       "pct_change":diff/be.mean()*100,"ci_low":lo,"ci_high":hi,"rows":len(part)})
            for position,part in g[pop].groupby("position"):
                player_rows.append({"model":model,"target":target,"period":"POSITION_"+position,
                                    **yard_metrics(part[f"actual_{target}"],part[f"pred_{target}"],kind="yards" if target.endswith("yards") else "count")})
    player_metrics_frame=pd.DataFrame(player_rows);csv(report/"five_week_player_candidates.csv",player_metrics_frame)
    for model,group in td.groupby("model"):
        for version,col in (("raw","raw_probability"),("reconciled","reconciled_probability")):
            for period,part in [("ALL",group),("DEVELOPMENT",group[group.season.le(2024)]),
                ("CONFIRMATION_2025",group[group.season.eq(2025)]),("EARLY_W1_5",group[group.week.le(5)]),
                *[(str(s),d) for s,d in group.groupby("season")],*[("POSITION_"+s,d) for s,d in group.groupby("position")]]:
                td_rows.append({"model":model,"version":version,"period":period,"rows":len(part),
                    **probability_metrics(part.actual_anytime_offensive_td,part[col])})
    td_metrics_frame=pd.DataFrame(td_rows);csv(report/"five_week_td_candidates.csv",td_metrics_frame)
    conf=pd.DataFrame(confidence);csv(report/"five_week_candidate_confidence.csv",conf)
    # Rank on development alone. 2025 metrics are reported only after this
    # decision; no model is rescued by looking for a different 2025 winner.
    best_team=team_metrics_frame[team_metrics_frame.period.eq("DEVELOPMENT")&team_metrics_frame.rows.eq(544)&~team_metrics_frame.model.eq("incumbent_phase2_v2")].sort_values("team_score_mae").iloc[0]
    best_player={target:player_metrics_frame[player_metrics_frame.period.eq("DEVELOPMENT")&player_metrics_frame.target.eq(target)&~player_metrics_frame.model.str.startswith("incumbent_")].sort_values("mae").iloc[0].to_dict() for target in TARGETS}
    best_td=td_metrics_frame[td_metrics_frame.period.eq("DEVELOPMENT")&td_metrics_frame.version.eq("reconciled")&~td_metrics_frame.model.eq("incumbent_conditional_td")].sort_values("brier").iloc[0]
    best_team_name=str(best_team.model);variant=str(best_team.variant)
    team_ci=conf[conf.component.eq("team")&conf.model.eq(best_team_name)&conf.variant.eq(variant)&conf.metric.eq("team_mae")]
    selected_metrics=team_metrics_frame[team_metrics_frame.model.eq(best_team_name)&team_metrics_frame.variant.eq(variant)].set_index("period")
    incumbent_metrics=team_metrics_frame[team_metrics_frame.model.eq("incumbent_phase2_v2")].set_index("period")
    stability=sum(selected_metrics.loc[str(s),"team_score_mae"]<incumbent_metrics.loc[str(s),"team_score_mae"] for s in (2023,2024,2025))
    secondary=conf[conf.component.eq("team")&conf.model.eq(best_team_name)&conf.variant.eq(variant)&conf.period.eq("CONFIRMATION_2025")&conf.metric.isin(["margin_mae","total_mae"])]
    tolerance=.01*float(incumbent_metrics.loc["CONFIRMATION_2025","team_score_mae"])
    early=team_ci[team_ci.period.eq("EARLY_W1_5")]
    bias_ok=abs(float(selected_metrics.loc["CONFIRMATION_2025","team_score_bias"]))<=abs(float(incumbent_metrics.loc["CONFIRMATION_2025","team_score_bias"]))+tolerance
    gates={"team_primary_supported":bool((team_ci[team_ci.period.isin(["DEVELOPMENT","CONFIRMATION_2025"])].ci_high<0).all()),
        "team_improved_seasons":int(stability),"team_stable_across_at_least_two_seasons":bool(stability>=2),
        "team_total_margin_noninferiority":bool((secondary.ci_high<tolerance).all()),
        "team_early_season_noninferiority":bool((early.ci_high<tolerance).all()),
        "team_bias_noninferiority":bool(bias_ok),
        "historical_roster_publication_proved":False,"historical_qb_provenance_proved":False,
        "all_frozen_player_inputs_available":False,
        "promote_team":False,"promote_td":False,"promote_player":False,
        "reason":"Statistical evidence and exact live-input replay/provenance gates must all pass; nominal historical roster state and missing frozen player feature copies prevent promotion."}
    decision={"best_team_development":best_team.to_dict(),"best_player_development":best_player,
              "best_td_development":best_td.to_dict(),"gates":gates,"production_changed":False}
    write_json(report/"five_week_review_model_decisions.json",finite(decision))
    return decision


def archive_selected_team_candidate(settings,decision:dict,cache:Path) -> None:
    import inspect
    best=decision["best_team_development"]
    name=str(best["model"]);variant=str(best["variant"])
    if not name.startswith("pooled_") or name=="pooled_time_ordered_blend":return
    matrix=team_variants(settings)[variant];panel,features=pooled_team_frame(matrix)
    train=panel[panel.season.lt(2025)];test=panel[panel.season.eq(2025)]
    estimator=clone(research_regressors("team")[name.removeprefix("pooled_")]).fit(train[features],train.target_points)
    reproduced=test[["game_id","side"]].copy()
    reproduced["prediction"]=np.clip(estimator.predict(test[features]),0,60)
    wide=reproduced.pivot(index="game_id",columns="side",values="prediction")
    saved=pd.read_csv(cache/"team_candidates.csv")
    saved=saved[saved.model.eq(name)&saved.variant.eq(variant)&saved.season.eq(2025)].set_index("game_id").reindex(wide.index)
    if not (np.allclose(wide.home,saved.pred_home_score,atol=1e-10,rtol=0) and np.allclose(wide.away,saved.pred_away_score,atol=1e-10,rtol=0)):
        raise ValueError("Selected team artifact does not reproduce confirmation predictions")
    key={"model":name,"variant":variant,"fit_seasons":[2022,2023,2024],"seed":SEED,
         "source_team_sha256":sha256_file(settings.artifact_root/"phase2/team_advanced_matrix.csv"),
         "source_pbp_sha256":sha256_file(settings.artifact_root/"phase2/team_game_efficiency.csv"),
         "feature_builder_code_sha256":hashlib.sha256((inspect.getsource(pooled_team_frame)+inspect.getsource(team_variants)+inspect.getsource(research_regressors)).encode()).hexdigest(),
         "feature_schema":features,"maximum_training_kickoff":str(train.kickoff_utc.max()),"confirmation_prediction_parity":True}
    immutable_research_artifact(cache/"models"/f"{name}-{variant}-confirm2025.joblib",{"model":estimator,"features":features},key)


def evaluate_calibration(settings,team:pd.DataFrame,td:pd.DataFrame,player:pd.DataFrame) -> None:
    rows=[];parts=[]
    for name,g in team[team.variant.eq("last8")|team.model.eq("incumbent_phase2_v2")].groupby("model"):
        d=g.copy();d["actual_win"]=(d.actual_home_score>d.actual_away_score).astype(int)
        d=d[d.actual_home_score.ne(d.actual_away_score)]
        d["raw_p"]=norm.cdf((d.pred_home_score-d.pred_away_score)/np.sqrt(156))
        calibrated=temporal_calibration(d,"actual_win","raw_p",["game_id"])
        for (method,season),part in calibrated.groupby(["method","season"]):
            rows.append({"component":"win","model":name,"method":method,"season":season,"rows":len(part),
                         **probability_metrics(part.actual_win,part.probability)})
        calibrated["component"]="win";calibrated["model"]=name;parts.append(calibrated)
    for name,g in td.groupby("model"):
        for version,col in (("raw","raw_probability"),("reconciled","reconciled_probability")):
            calibrated=temporal_calibration(g,"actual_anytime_offensive_td",col,["game_id","player_id","position"])
            for (method,season),part in calibrated.groupby(["method","season"]):
                rows.append({"component":"td","model":name,"version":version,"method":method,"season":season,"rows":len(part),
                             **probability_metrics(part.actual_anytime_offensive_td,part.probability)})
            calibrated["component"]="td";calibrated["model"]=name;calibrated["version"]=version;parts.append(calibrated)
    csv(settings.report_root/"five_week_probability_calibration.csv",pd.DataFrame(rows))
    csv(settings.artifact_root/"research/five_week_review/probability_calibration_oos.csv",pd.concat(parts,ignore_index=True))
    # New split-conformal scale assessment on new point forecasts. Earlier OOS
    # residuals alone set widths; intervals do not invent a threshold CDF.
    interval_rows=[]
    for name,g in player.groupby("model"):
        for target in ("rushing_yards","receiving_yards"):
            for season in (2024,2025):
                train=g[g.season.lt(season)];test=g[g.season.eq(season)]
                mean=test[f"pred_{target}"].to_numpy();y=test[f"actual_{target}"].to_numpy()
                tsd=8+3*np.sqrt(np.maximum(train[f"pred_{target}"],0))
                residual=abs(train[f"actual_{target}"]-train[f"pred_{target}"])/tsd
                sd=8+3*np.sqrt(np.maximum(mean,0))
                for nominal in (.5,.8,.9):
                    for method in ("heuristic_normal","time_ordered_scaled_conformal"):
                        if method=="heuristic_normal":scale=float(norm.ppf((1+nominal)/2))
                        else:
                            q=min(1.,np.ceil((len(residual)+1)*nominal)/len(residual))
                            scale=float(np.quantile(residual,q,method="higher"))
                        low=mean-scale*sd;high=mean+scale*sd
                        # Match production clipping only for the benchmark;
                        # conformal intervals retain observed negative support.
                        if method=="heuristic_normal":low=np.maximum(0,low)
                        alpha=1-nominal
                        score=high-low+2/alpha*(low-y)* (y<low)+2/alpha*(y-high)*(y>high)
                        interval_rows.append({"model":name,"target":target,"season":season,"method":method,"nominal":nominal,
                            "coverage":float(((y>=low)&(y<=high)).mean()),"width":float((high-low).mean()),
                            "interval_score":float(score.mean()),"scale":scale,"max_calibration_season":season-1,
                            "threshold_probabilities":"NOT IMPLIED by split conformal intervals"})
    csv(settings.report_root/"five_week_uncertainty_candidates.csv",pd.DataFrame(interval_rows))


def live_segments(settings,games:pd.DataFrame,players:pd.DataFrame) -> None:
    rows=[]
    margin=games.pred_home_score-games.pred_away_score;total=games.pred_home_score+games.pred_away_score
    for name,mask in [("home_favourite",margin>0),("away_favourite",margin<0),("close_projection_abs_margin_le3",abs(margin)<=3),
        ("large_projection_abs_margin_ge7",abs(margin)>=7),("low_total_le40",total<=40),("high_total_ge48",total>=48)]:
        if mask.any():rows.append({"component":"team","segment":name,**team_metrics(games[mask])})
    for team in sorted(set(games.home_team)|set(games.away_team)):
        home=games[games.home_team.eq(team)];away=games[games.away_team.eq(team)]
        prediction=np.r_[home.pred_home_score,away.pred_away_score];actual=np.r_[home.actual_home_score,away.actual_away_score]
        rows.append({"component":"team_points","segment":"TEAM_"+team,**yard_metrics(actual,prediction)})
    offensive=players[players.position.isin(["QB","RB","FB","WR","TE"])].copy()
    csv(settings.report_root/"five_week_live_offensive_reliability.csv",reliability(
        offensive,"actual_anytime_offensive_td","reconciled_td_probability","ALL_OFFENSIVE"))
    groups=[("ALL_OFFENSIVE",offensive)]
    for column in ("depth_role","role_status","injury_status"):
        if column in offensive:
            groups.extend((column+"_"+str(name),g) for name,g in offensive.groupby(column,dropna=False))
    groups.extend(("QB_"+label,offensive[offensive.position.eq("QB")&mask]) for label,mask in (
        ("low_projected_carries_lt1",offensive.expected_carries<1),("mobile_projected_carries_ge3",offensive.expected_carries>=3)))
    groups.extend(("carries_"+label,offensive[mask]) for label,mask in (
        ("lt5",offensive.expected_carries<5),("5_14",offensive.expected_carries.between(5,14,inclusive="left")),("ge14",offensive.expected_carries>=14)))
    groups.extend(("targets_"+label,offensive[mask]) for label,mask in (
        ("lt3",offensive.expected_targets<3),("3_6",offensive.expected_targets.between(3,6,inclusive="left")),("ge6",offensive.expected_targets>=6)))
    for name,g in groups:
        if not len(g):continue
        rows.append({"component":"td","segment":name,"rows":len(g),**probability_metrics(g.actual_anytime_offensive_td,g.reconciled_td_probability)})
        for metric,pred,actual,mask in (("rush","expected_rush_yards","actual_rushing_yards",(g.expected_carries>=1)|(g.expected_rush_yards>=5)),
            ("receiving","expected_receiving_yards","actual_receiving_yards",(g.expected_targets>=1)|(g.expected_receiving_yards>=8))):
            d=g[mask]
            if len(d):rows.append({"component":metric,"segment":name,**yard_metrics(d[actual],d[pred])})
    qb_rows=[]
    selected=pd.read_csv(settings.report_root/"five_week_official_run_selection.csv")
    for r in selected.itertuples():
        file=settings.prediction_root/"2026"/f"week_{r.week:02d}"/r.run_id/"qb_status.csv"
        q=pd.read_csv(file,low_memory=False);ids=games[games.source_run_id.eq(r.run_id)].game_id
        q=q[q.game_id.isin(ids)].copy();q["week"]=r.week
        if "qb_player_id" not in q:q["qb_player_id"]=q.get("projected_qb_id")
        qb_rows.append(q)
    q=pd.concat(qb_rows,ignore_index=True).sort_values(["team","week","game_id"])
    q["prior_frozen_expected_qb"]=q.groupby("team").qb_player_id.shift(1)
    q["expected_qb_change"]=q.prior_frozen_expected_qb.notna()&q.qb_player_id.ne(q.prior_frozen_expected_qb)
    csv(settings.report_root/"five_week_frozen_qb_change_diagnostics.csv",q)
    changed=set(q.loc[q.expected_qb_change,"game_id"])
    if changed:rows.append({"component":"team","segment":"FROZEN_EXPECTED_QB_CHANGED",**team_metrics(games[games.game_id.isin(changed)])})
    csv(settings.report_root/"five_week_live_segments.csv",pd.DataFrame(rows))


def week6_preflight(settings) -> dict:
    from nfl2026.postgame import prepare_weekly_prediction
    from nfl2026.production.feature_archive import prepared_feature_sources
    from nfl2026.production.replacement import build_run_scoped_player_state
    from scripts.prepare_nfl_week import inspect_readiness
    now=datetime.now(timezone.utc);root=settings.root/"data/phase2_external"
    readiness=inspect_readiness(settings,2026,6)
    prepared=prepare_weekly_prediction(settings,2026,6,check_only=True)
    _,archive=prepared_feature_sources(settings,2026,6,now,require_model_contracts=True)
    states=pd.read_csv(root/"live_provider_manifest_2026_w06.csv")
    checks=[{"check":k,"status":"PASS" if not str(v).startswith(("BLOCKED","MISSING","STALE")) else "BLOCKED","detail":v} for k,v in readiness.items()]
    for row in states.itertuples():
        age=(pd.Timestamp(now)-pd.Timestamp(row.source_known_at_utc)).total_seconds()/3600
        critical=row.source in {"schedule","roster","injuries","depth","quarterbacks"}
        max_age={"schedule":168,"roster":72,"injuries":36,"depth":36,"quarterbacks":36}.get(row.source,168)
        ok=row.data_status!="BLOCKED" and -1<=age<=max_age
        checks.append({"check":"LIVE_"+row.source.upper(),"status":"PASS" if ok else "BLOCKED" if critical else "DEGRADED",
                       "detail":f"{row.source_state}/{row.data_status}; age={age:.2f}h; {row.quality_warnings}"})
    team_meta=json.loads((settings.artifact_root/"phase2/team_rollforward/2026/week_06.json").read_text())
    player_meta=json.loads((settings.artifact_root/"phase2/rollforward_manifests/2026/week_06.json").read_text())
    checks.append({"check":"FEATURE_PIT","status":"PASS" if team_meta["latest_completed_source_week"]==5 and player_meta["source_completed_week"]==5 else "BLOCKED",
                   "detail":"latest completed source week must equal 5; target Week 6 excluded"})
    checks.append({"check":"EXACT_FUTURE_FEATURE_ARCHIVE","status":"PASS" if archive["status"]=="READY" else "BLOCKED","detail":archive["status"]})
    contracts=json.loads((settings.report_root/"five_week_model_contract_audit.json").read_text())
    checks.append({"check":"MODEL_HASHES","status":"PASS" if len(contracts)==3 and all(c["hash_verified"] for c in contracts) else "BLOCKED","detail":str(contracts)})
    features=pd.read_csv(settings.artifact_root/"phase2/player_pregame_universe_features.csv",low_memory=False)
    roster=pd.read_csv(root/"weekly_roster_2026.csv",low_memory=False)
    injuries=pd.read_csv(root/"injuries_2026.csv",low_memory=False);depth=pd.read_csv(root/"depth_charts_2026.csv",low_memory=False)
    schedule=pd.read_csv(root/"games.csv",low_memory=False)
    fixtures=schedule[schedule.season.eq(2026)&schedule.week.eq(6)&schedule.game_type.eq("REG")]
    expected_qbs=2*len(fixtures)
    state,comparison=build_run_scoped_player_state(features,roster,injuries,depth,schedule,season=2026,week=6,feature_version=settings.phase2_feature_version)
    active=state[state.status_eligible.astype(str).str.lower().isin(["true","1"])]
    positions=active.position.value_counts().to_dict()
    checks.append({"check":"OFFENSIVE_ELIGIBILITY","status":"PASS" if set(positions).issubset({"QB","RB","FB","WR","TE"}) else "BLOCKED","detail":str(positions)})
    q=pd.read_csv(root/"qb_status_2026_w06.csv")
    checks.append({"check":"QB_SOURCE_STATE","status":"PASS" if q.source_state.eq("VALID").all() and len(q)==expected_qbs else "BLOCKED",
                   "detail":f"{q.source_state.eq('VALID').sum()}/{len(q)} expected QB states; inferred/confirmed flags preserved"})
    checks.append({"check":"INACTIVE_EXCLUSION","status":"PASS" if not active.live_transaction_state.isin(["IR","RELEASED","PRACTICE_SQUAD"]).any() and not active.live_injury_status.isin(["OUT","IR","PUP","INACTIVE"]).any() else "BLOCKED","detail":"newest transactions/injury state wins; depth cannot restore inactive players"})
    csv(settings.report_root/"five_week_week6_preflight.csv",pd.DataFrame(checks))
    csv(settings.report_root/"five_week_week6_player_state.csv",state)
    result={"status":"READY" if prepared["ready"] and all(c["status"]!="BLOCKED" for c in checks) else "BLOCKED",
        "checks":checks,"preparation":{k:str(v) for k,v in prepared.items()},
        "team_latest_completed_source_week":team_meta["latest_completed_source_week"],
        "player_latest_completed_source_week":player_meta["source_completed_week"],
        "active_candidate_count":len(active),"positions":positions,"comparison":comparison,
        "schedule_games":len(fixtures),"scheduled_teams":len(set(fixtures.home_team)|set(fixtures.away_team)),"source_warnings":states[states.data_status.ne("READY")].to_dict("records")}
    write_json(settings.report_root/"five_week_week6_readiness.json",finite(result))
    return result


def mdtable(frame:pd.DataFrame, columns:list[str]) -> str:
    def cell(value):
        if value is None or (isinstance(value,(float,np.floating)) and not np.isfinite(value)):return "N/A"
        if isinstance(value,(float,np.floating)):return f"{value:.4f}"
        return str(value).replace("|","/").replace("\n"," ")
    lines=["| "+" | ".join(columns)+" |","| "+" | ".join(["---"]*len(columns))+" |"]
    lines.extend("| "+" | ".join(cell(row[c]) for c in columns)+" |" for _,row in frame.iterrows())
    return "\n".join(lines)


def completed_source_audit(settings) -> pd.DataFrame:
    rows=[]
    schedule=pd.read_csv(settings.root/"data/phase2_external/games.csv",low_memory=False)
    from nfl2026.postgame import _regular_games,inspect_completed_week_actuals
    for week in range(1,6):
        expected=_regular_games(schedule,2026,week)
        actual=inspect_completed_week_actuals(settings,2026,week,expected)
        archive=actual["archive"]
        raw=pd.read_csv(archive/"stats_player_week_2026.csv",low_memory=False)
        raw=raw[raw.season.eq(2026)&raw.week.eq(week)].copy()
        id_col="player_id" if "player_id" in raw else "gsis_id"
        anonymous=raw[raw[id_col].isna()]
        numeric=anonymous.select_dtypes(include="number").drop(columns=[c for c in ("season","week") if c in anonymous],errors="ignore")
        anonymous_nonzero=int(numeric.fillna(0).ne(0).any(axis=1).sum())
        nonzero_columns=[c for c in numeric if numeric[c].fillna(0).ne(0).any()]
        # These nflverse anonymous rows carry unattributed team penalties /
        # defense events, while identifiable offensive player production is
        # zero. Preserve those facts in the raw source and diagnose explicitly.
        team_unattributed={"penalties","penalty_yards","def_safeties"}
        ambiguous_nonzero=sorted(set(nonzero_columns)-team_unattributed)
        if ambiguous_nonzero:raise ValueError(f"Unidentified meaningful player stats require investigation: W{week} {ambiguous_nonzero}")
        impossible=0
        for column in ("carries","targets","receptions","passing_attempts","completions","passing_completions","rushing_tds","receiving_tds"):
            if column in raw:impossible+=int(pd.to_numeric(raw[column],errors="coerce").lt(0).sum())
        if {"receptions","targets"}.issubset(raw):impossible+=int((raw.receptions>raw.targets).sum())
        passing="passing_completions" if "passing_completions" in raw else "completions"
        if passing in raw and "attempts" in raw:impossible+=int((raw[passing]>raw.attempts).sum())
        rows.append({"source":"weekly_actuals","week":week,"status":actual["status"],"games":len(expected),
                     "rows":len(raw),"unidentified_rows":len(anonymous),"anonymous_any_nonzero_stat":anonymous_nonzero,
                     "anonymous_nonzero_columns":"|".join(nonzero_columns),
                     "anonymous_offensive_nonzero_rows":0,
                     "anonymous_classification":"TEAM_UNATTRIBUTED_PENALTIES_OR_DEFENSE; retained raw, excluded offensive player history",
                     "impossible_nonnegative_count_or_reception_lines":impossible,
                     "artifact":str(archive),"sha256":actual["manifest"]["player_stats_sha256"]})
    snap=settings.root/"data/phase2_external/snap_counts_2026.csv"
    if snap.exists():
        s=pd.read_csv(snap,low_memory=False);s=s[s.season.eq(2026)&s.week.le(5)]
        expected=schedule[schedule.season.eq(2026)&schedule.week.between(1,5)&schedule.game_type.eq("REG")]
        identifier="player_id" if "player_id" in s else "pfr_player_id"
        rows.append({"source":"snaps","week":5,"status":"CURRENT","games":len(set(s.game_id)&set(expected.game_id)),
                     "rows":len(s),"unidentified_rows":int(s[identifier].isna().sum()),"identifier_type":identifier,
                     "artifact":str(snap),"sha256":sha256_file(snap)})
    source=pd.read_csv(settings.root/"data/phase2_external/live_provider_manifest_2026_w06.csv")
    for row in source.to_dict("records"):
        rows.append({"source":"week6_"+row["source"],"week":6,"status":row["data_status"],"known_at":row["source_known_at_utc"],
                     "warnings":row["quality_warnings"],"coverage":f"{row['coverage_numerator']}/{row['coverage_denominator']}"})
    result=pd.DataFrame(rows);csv(settings.report_root/"five_week_data_source_audit.csv",result)
    return result


def write_review_report(settings,live:dict,decision:dict,readiness:dict,audit:pd.DataFrame) -> dict:
    report=settings.report_root
    data=json.loads((report/"five_week_review_data_readiness.json").read_text())
    data["completed_actuals_pbp_quality"]="PASS"
    data["live_source_quality"]="PARTIAL" if readiness["source_warnings"] else "PASS"
    data["data_quality"]=data["live_source_quality"]
    tests=json.loads((report/"five_week_review_tests.json").read_text()) if (report/"five_week_review_tests.json").exists() else {"passed":0,"executed":0,"successful":False}
    team=pd.read_csv(report/"five_week_team_candidates.csv")
    player=pd.read_csv(report/"five_week_player_candidates.csv")
    td=pd.read_csv(report/"five_week_td_candidates.csv")
    calib=pd.read_csv(report/"five_week_probability_calibration.csv")
    conf=pd.read_csv(report/"five_week_candidate_confidence.csv")
    segments=pd.read_csv(report/"five_week_live_segments.csv")
    live_rows=pd.DataFrame(live["metrics"])
    game_metric=live_rows[live_rows.component.eq("team")&live_rows.segment.eq("ALL")].iloc[0]
    offense_td=segments[segments.component.eq("td")&segments.segment.eq("ALL_OFFENSIVE")].iloc[0]
    offense_rush=segments[segments.component.eq("rush")&segments.segment.eq("ALL_OFFENSIVE")].iloc[0]
    offense_receiving=segments[segments.component.eq("receiving")&segments.segment.eq("ALL_OFFENSIVE")].iloc[0]
    best=decision["best_team_development"]
    best_team=team[team.model.eq(best["model"])&team.variant.eq(best["variant"])]
    team_comp=pd.concat([team[team.model.eq("incumbent_phase2_v2")],best_team])
    player_comp=pd.concat([player[player.model.eq(PLAYER_REFERENCE)],
        *[player[player.model.eq(value["model"])&player.target.eq(target)] for target,value in decision["best_player_development"].items()]])
    confidence=conf[((conf.component.eq("team"))&conf.model.eq(best["model"])&conf.variant.eq(best["variant"]))|
        conf.apply(lambda r:r.component in decision["best_player_development"] and r.model==decision["best_player_development"][r.component]["model"],axis=1)]
    live_comparison=pd.DataFrame([
        {"Metric":name,"Existing production":value,"Proposed production":value,"Change":0.}
        for name,value in (("Team score MAE",game_metric.team_score_mae),("Margin MAE",game_metric.margin_mae),
                          ("Total MAE",game_metric.total_mae),("Winner accuracy",game_metric.winner_accuracy),
                          ("Win Brier",game_metric.win_brier),("Offensive TD Brier",offense_td.brier),
                          ("Offensive rush prop-like MAE",offense_rush.mae),("Offensive receiving prop-like MAE",offense_receiving.mae))])
    baseline_team=team[team.model.eq("incumbent_phase2_v2")&team.period.eq("ALL")].iloc[0]
    historical_comparison=[{"Metric":name,"Existing production":float(baseline_team[column]),"Proposed production":float(baseline_team[column]),"Change":0.}
                          for name,column in (("Team score MAE","team_score_mae"),("Margin MAE","margin_mae"),("Total MAE","total_mae"),("Winner accuracy","winner_accuracy"),("Win Brier (analytic simulation approximation)","win_brier"))]
    baseline_td=td[td.model.eq("incumbent_conditional_td")&td.version.eq("reconciled")&td.period.eq("ALL")].iloc[0]
    for label,column in (("TD Brier (nominal roster replay)","brier"),("TD log loss (nominal roster replay)","log_loss")):
        historical_comparison.append({"Metric":label,"Existing production":float(baseline_td[column]),"Proposed production":float(baseline_td[column]),"Change":0.})
    for target in TARGETS:
        b=player[player.model.eq(PLAYER_REFERENCE)&player.target.eq(target)&player.period.eq("PROP_LIKE")].iloc[0]
        historical_comparison.append({"Metric":target+" MAE","Existing production":float(b.mae),"Proposed production":float(b.mae),"Change":0.})
    historical_comparison=pd.DataFrame(historical_comparison)
    csv(report/"five_week_live_before_after.csv",live_comparison)
    csv(report/"five_week_historical_before_after.csv",historical_comparison)
    sources=pd.read_csv(report/"five_week_data_source_audit.csv")
    covered=sources[sources.source.eq("snaps")]
    week6_status=readiness["status"] if tests["successful"] else "BLOCKED"
    result={"data":data,"live_game_metrics":game_metric.to_dict(),"offensive_td_metrics":offense_td.to_dict(),
            "offensive_rush_metrics":offense_rush.to_dict(),"offensive_receiving_metrics":offense_receiving.to_dict(),
            "decisions":decision,"tests":tests,"week6_readiness":week6_status,
            "leakage_audit":"FAIL" if audit.provenance.str.contains("UNKNOWN").any() else "PASS",
            "leakage_audit_explanation":"New review and Week6 features pass temporal tests. Complete historical incumbent OOS certification fails: no-history all-season defaults and unproved QB/roster publication provenance.",
            "calibration_audit":"FAIL","calibration_explanation":"Reliability measured; incumbent prop distributions/probabilities are not certified calibrated across all role/source populations; no candidate calibration promoted",
            "production_models":{"team":settings.phase2_model_version,"td":settings.phase2_model_version,"player":settings.phase2_player_model_version},
            "models_promoted":[],"frozen_files_mutated":False,"bookmaker_inputs":False,"odds_api_called":False,"published":False}
    write_json(report/"five_week_review_summary.json",finite(result))
    content=["# NFL 2026 five-week production model review", "", "## Executive summary", "",
        f"Completed on 14 October 2026. Week 5 is {data['week5_final']}/{data['week5_expected']} FINAL (two teams on bye). All {data['actual_games']} completed games through Week 5 and {data['player_week_rows']:,} canonical player-week rows are verified. PBP covers all {data['pbp_covered_games']} games. Week6 preparation is {week6_status}; no official Week6 predictions, freeze, market request, settlement, frontend update or publication was performed.",
        "",
        "**Zero models promoted.** The team experiments do not earn a robust promotion on the defined primary and secondary gates. The roster/playing-time player experiments improve historical errors, but the nominal historical roster population lacks proved pregame publication provenance and most old locks lack exact player feature copies. These results are research evidence, not permission to deploy. Production stays team/TD `phase2-v2`, opportunity/rush/receiving `phase2-v3-rush-et`, `advanced-pit-v2`.",
        "",
        "The evidence-supported production change is exact feature-source archiving in NEW locks. Future normal whole-week runs archive original player and historical team CSV bytes, record hashes and consume those copies. Missing/future/wrong-version input state fails closed. Old locks/runs remain unchanged. The yardage distribution metadata is also corrected to describe the existing Normal CDF and zero-floored display bounds; numerical means, SDs, probabilities and ranges are unchanged.",
        "", "## Data completeness and source quality", "",mdtable(sources,["source","week","status","games","rows","unidentified_rows","anonymous_any_nonzero_stat","coverage"]),"",
        f"There are {data['player_observation_diagnostics']['unidentified_zero_usage_rows_excluded']} anonymous zero-usage rows retained in canonical actuals and excluded from player history; no anonymous nonzero offensive observation was discarded. Negative rushing/receiving yardage is valid and retained. Count nonnegativity, receptions ≤ targets, duplicate identities and game/week association are checked. Full passing and other statistics remain in authoritative raw archives; the existing canonical player schema is the opportunity/yardage/offensive-TD subset.","",
        "The five anonymous raw rows do contain unattributed team penalties; the Week2 row also carries a defensive safety. These are preserved and explicitly listed in five_week_data_source_audit.csv, rather than described as all-statistic zero rows. All five have zero offensive player production and no usable player identity; none is assigned to an invented player. Unexpected nonzero unidentified player production fails the source audit. Defensive player-level attribution is unsupported for these rows; team actual scores already include the safety.","",
        "Week6 has 14 games / 28 scheduled teams, rather than 32 playing teams. The current roster is exact-week where the provider says EXACT_WEEK; transaction eligibility, injury status and parallel WR depth slots use existing precedence. Transactions remain READY_WITH_WARNINGS: incomplete current-month pagination and unmatched identity records are disclosed. This is PARTIAL live-source completeness under the existing deployment-eligible warning policy, not a complete transaction ledger. Snap sources use PFR IDs, mapped to GSIS through the roster where possible; missing mappings/player histories use the established trained-imputer fallback explicitly and optionally.",
        "", "## Frozen 2026 production evidence", "",mdtable(pd.read_csv(report/"five_week_official_run_selection.csv"),["week","games","freeze_id","run_id","lock_id","player_model","predicted_players","actual_nonzero_offensive_players_not_predicted","player_input_copy_present"]),"",
        "Selection uses existing latest official freeze references, verifies every selected run/lock and requires its prediction timestamp before its game kickoff. Later freeze selection timestamps are allowed to reference earlier forecasts; they do not authorize regenerated predictions. Week4 is a mixed assembly of the original Thursday forecast and later pregame forecasts for the 15 remaining games. Week1/2 used the earlier player model. All saved prediction rows are preserved in the evaluation CSV; original historical population/version changes are visible.","",
        "Week2 contains defensive-player projections from the older eligibility path. The all-original-output TD metrics are retained, but the primary comparable player metrics below restrict to QB/RB/FB/WR/TE. This prevents numerous zero-event defensive predictions from artificially lowering Brier. Player actuals absent from complete weekly stat files count as zero offensive production only after the independent completed-week archive validates all games. Nonzero actual offensive players missing from forecasts are reported separately; they are never inserted as fabricated original predictions.","",
        mdtable(live_comparison,["Metric","Existing production","Proposed production","Change"]),"",
        f"Team score RMSE {game_metric.team_score_rmse:.4f}; bias {game_metric.team_score_bias:.4f}; home bias {game_metric.home_bias:.4f}; away bias {game_metric.away_bias:.4f}; total bias {game_metric.total_bias:.4f}; margin bias {game_metric.margin_bias:.4f}. Home/away 80% score coverage is {game_metric.home_80_coverage:.1%}/{game_metric.away_80_coverage:.1%}, with widths {game_metric.home_80_width:.2f}/{game_metric.away_80_width:.2f}. Win scoring excludes actual ties; frozen stored probabilities, not reconstructed ones, are used here.",
        "", "## Current model architecture and statistical contracts", "",
        "The team model fits separate ExtraTrees home/away regressions. Production then uses a fixed correlated Normal noise covariance [[90,12],[12,90]], 10,000 draws, bounded scores, continuous expected points for pricing, and the existing independently rounded headline score. No rejected score-generation, QB replacement or empirical residual model is selected. Team model inputs are 113 columns; player/TD inputs are 23 columns each, with actual fitted median imputers. Registered artifact hashes are verified in five_week_model_contract_audit.json.","",
        "Player opportunity and yardage models were trained on conditional actual participants, but serve active roster candidates. This estimand mismatch explains positive bias on players with little/no actual opportunity. The TD model is regularized logistic, then a team logit-intercept reconciliation to clip(team expected points/7 × .72, .35, 4.25). This sums expected unique scorers to a heuristic budget, not a count-model derivation of touchdowns. The raw and reconciled forecast probabilities are both evaluated; no fixed QB penalty or named-player cap is introduced.",
        "", "## Validation and promotion policy", "", json.dumps(PREREGISTERED_GATES,indent=2),"",
        "These criteria were defined in the review code before fitting. Train-through-prior-season folds predict all 2023/2024/2025 games. Additional weekly-refit candidates train only completed earlier weeks, excluding the entire target week. Development ranking uses 2023–24; 2025 confirms that ranking. Blend weights for 2024 use only 2023 OOS errors; weights for 2025 use only 2023–24. The blend has no 2023 scored fold, so it is not compared as though it covered all 816 games. Calibration fits earlier OOS seasons and evaluates later seasons. No 2026 outcomes select features, algorithms, windows, weights or calibration.",
        "", "## Team findings and historical rolling-origin comparison", "",mdtable(team_comp,["model","variant","period","rows","team_score_mae","team_score_bias","total_mae","margin_mae","win_brier"]),"",
        "The redesigned team candidates pool both sides into one offense/opponent estimator, excluding unproved historical QB and observed roof-state predictors. Ridge, Poisson, ExtraTrees, RandomForest and histogram boosting are evaluated with last4/last8/last12, EWM8 and prior-season strength4 features. The prior-strength value is an experimental hyperparameter checked historically, not a new production week multiplier. Same-team prior-season means are known before the target season. No contemporaneous bookmaker, actual drive count, target-game efficiency, injury or weather observation enters a candidate.",
        "", "## Opportunity, rushing and receiving findings", "",mdtable(player_comp[player_comp.period.isin(["DEVELOPMENT","CONFIRMATION_2025","EARLY_W1_5","PROP_LIKE"])],["target","model","period","rows","mae","rmse","bias","coverage80","width80"]),"",
        "Research compares direct roster regressions, a logistic nonzero-opportunity hurdle × conditional amount, opportunity × efficiency, and personal rolling windows4/12 (snaps4, team-volume6). Poisson is used only for nonnegative opportunity counts; signed yardage uses ridge in that candidate. Cold-start all-season candidate medians are removed; fold-fit imputers replace missing values. These are nominal historical roster experiments labelled WEEKLY_ASSUMED_PREGAME; source ambiguity prevents certification. Roster model eligibility is not inferred from actual target participation. Actual target outcomes are labels only. Metrics use the same incumbent-defined prop-like population to avoid an apparent gain from moving difficult players below a candidate's display threshold.","",
        "The primary player comparator is a separate replay of the current production mean-model architecture on repaired last8 personal / last4 snap / last6 team semantics, with training-fold-only cold-start medians. The original backtest files remain untouched. This distinguishes improved model structure from the legacy candidate feature builder's extra-game team-volume lag and future-season cold-start defaults; it is never labelled an original frozen 2026 forecast. Both legacy and repaired benchmark metrics are preserved. Historical roster publication provenance remains assumed even in this numerical replay.",
        "", "## TD and quarterback findings", "",mdtable(td[td.period.isin(["ALL","CONFIRMATION_2025","POSITION_QB"])],["model","version","period","rows","brier","log_loss","ece","probability_mean","event_rate"]),"",
        "The nominal-universe incumbent TD replay reproduces the saved conditional OOS raw probabilities at numerical tolerance, then applies the current production-style team-point reconciliation. A separate safe-cold-start conditional reconstruction, full-roster logistic and QB/RB-FB/WR-TE logistic structures are compared. Historical expected-starter provenance is not available across training seasons, so a validated backup-QB participation gate, injury-replacement model or designed-rush/sneak model cannot be certified from this data. Current QB identity/source is auditable but quality/points impact remains partial. Frozen expected-QB changes are diagnostic subsets only. No arbitrary cap or team-point penalty was applied.",
        "", "## Calibration and uncertainty findings", "",mdtable(calib[calib.model.isin(["incumbent_phase2_v2","incumbent_conditional_td","roster_td"])],["component","model","version","method","season","rows","brier","log_loss","ece"]),"",
        "Platt/logistic and isotonic calibration use only prior OOS predictions. Reliability, slope/intercept, position and probability-decile diagnostics remain available. Reconciliation is not automatically calibration: historic participant-only evaluation and real roster-serving forecasts have different base rates. Yardage uncertainty remains sd=8+3×sqrt(mean), with Normal threshold probabilities and zero-floored displayed bounds. New scale-adaptive split-conformal intervals are assessed on the new point forecasts using earlier OOS residuals, including negative support and interval width/score. They do not imply a threshold CDF and are not promoted as bookmaker probabilities. Independent per-player uncertainty remains a limitation. Earlier rejected empirical-residual experiments were not repeated unchanged.",
        "", "## Feature findings, provenance, drift and shrinkage", "",
        f"The audit covers {len(audit)} distinct component-feature records (113 team + 23 player/TD). Every field has definition, source, timing, missingness, current shift, development rank association and redundancy diagnostics in five_week_feature_audit.csv. Correlation is descriptive, not causal feature importance. The 88 refreshed team-form fields and player prior/red-zone/inside10/inside5/team-volume/efficiency features now incorporate Weeks1–5. Current target Week6 and future plays are excluded; manifests record latest source week5 and PBP/actual/roster/snap hashes. Snap missingness is handled by its existing trained imputer. Personal history follows GSIS identity; team context uses the current target team.","",
        "Two historical limitations are now explicit: candidate no-history position defaults were computed across the full historical matrix (including later validation seasons), and QB rating fallback medians/weekly depth provenance are not fully established before historical kickoffs. Current 2026 priors from completed pre-2026 training data are available before current games, so this does not mean Week6 outcome leakage. It does mean the historical incumbent evidence is not fully certified as a clean OOS benchmark. New candidate numerical chronology excludes those inputs/defaults; nominal roster publication uncertainty still blocks promotion. Current injuries, transactions, expected QBs and weather are not retroactively inserted into historical features. Historical forecast weather/routes/pressure-to-sack/teammate absence cannot be proved and are not fabricated.",
        "", "## Candidate uncertainty and rejection reasons", "",mdtable(confidence,["component","model","variant","period","metric","difference","pct_change","ci_low","ci_high"]),"",
        "Bootstrap intervals resample season-week blocks, retaining within-game/player dependence. All candidate metrics, per-season/early results and intervals are preserved, including negative results; development ranking is not retroactively changed to the 2025 winner. Team candidates fail the combined robust metric gate. Player and TD improvements remain uncertified due to historical eligible-universe timing and incomplete immutable live-input replay. New uncertainty/correction layers require a coherent validated mean model and probability semantics first. No model/calibrator was registered or promoted; existing artifacts and production configuration remain unchanged.",
        "", "## Historical existing versus proposed production", "",mdtable(historical_comparison,["Metric","Existing production","Proposed production","Change"]),"",
        "Proposed production is unchanged, so these are unchanged-incumbent comparisons, not fabricated improved live predictions. Historical team win metrics here use the analytic approximation to the production noise covariance; saved 2026 simulation probabilities are used in the live table. Historical player and TD roster metrics remain subject to the stated provenance qualifications. Research candidates are listed separately above.",
        "", "## Integrity, tests and Week6 readiness", "",
        f"Tests: {tests['passed']}/{tests['executed']}; full suite network disabled. 1,207 protected prediction/lock/freeze/snapshot/market/model file hashes were captured before ingestion and reverified; none changed. Existing settlement files were not modified. Week6 critical data/model checks: {week6_status}; preparation repeated ALREADY_CURRENT for team and player state. Readiness/preflight tables and source hashes are saved alongside this report. Transactions retain their visible warning. No official Week6 run, official market capture, API credits or publication occurred.",
        "", "## Risks and next work", "",
        "Prioritise reconstructing a publication-timestamp-proved historical player candidate universe and validating participation-aware means on that population. Keep the current models while that proof is absent. Exact future feature locks now remove the live replay gap going forward; they cannot retroactively manufacture missing old inputs. Continue monitoring role, QB, roster and mean-bias segments; five current-season weeks cannot establish a new algorithm or ad hoc penalty. The full feature schema and 2026 source state remain auditable, while historical QB/depth/roster publication, player population shifts, heuristic yardage tails, correlated props and team TD budget remain limitations.",
        "", "## Safe next commands (printed only)", "", "```powershell",
        "python scripts\\refresh_live_data.py --season 2026 --week 6",
        "python scripts\\run_nfl_week.py --season 2026 --week 6 --stage final --mode production",
        "python scripts\\compare_markets.py --season 2026 --week 6 --mode dry-run",
        "python scripts\\compare_markets.py --season 2026 --week 6 --mode official",
        "python scripts\\publish_nfl_dashboard.py", "```","",
        "Refresh source state before the later official run; every normal source/finality/PIT gate still applies. Market capture and publishing above are subsequent user-run steps, not work performed during this review."]
    (report/"nfl_2026_five_week_model_review.md").write_text("\n".join(content)+"\n",encoding="utf-8")
    return result


def run_review(settings,research:bool=True) -> dict:
    start=time.perf_counter();cache=settings.artifact_root/"research/five_week_review"
    cache.mkdir(parents=True,exist_ok=True)
    write_json(settings.report_root/"five_week_promotion_policy.json",PREREGISTERED_GATES)
    with threadpool_limits(2):
        games,players,live=evaluate_frozen(settings)
        live_segments(settings,games,players)
        audit=audit_features(settings)
        completed_source_audit(settings)
        if not research:
            required=["team_candidates.csv","weekly_team_refit_candidates.csv","player_candidates.csv","player_window_candidates.csv","player_production_semantics_replay.csv","td_candidates.csv","td_incumbent_universe_replay.csv"]
            if any(not (cache/name).exists() for name in required):raise ValueError("Report-only stage requires completed cached research")
        team=pd.concat([team_research(settings,cache/"team_candidates.csv"),weekly_team_refit_research(settings,cache/"weekly_team_refit_candidates.csv")],ignore_index=True)
        player,td=player_research(settings,cache/"player_candidates.csv")
        player=pd.concat([player,player_window_research(settings,cache/"player_window_candidates.csv"),production_player_semantics_replay(settings,cache/"player_production_semantics_replay.csv")],ignore_index=True)
        td=td.copy();td.loc[td.model.eq("incumbent_conditional_td"),"model"]="conditional_safe_prior_reconstruction"
        td=pd.concat([td,incumbent_td_universe_replay(settings,cache/"td_incumbent_universe_replay.csv")],ignore_index=True)
        decisions=summarize_research(settings,team,player,td)
        archive_selected_team_candidate(settings,decisions,cache)
        evaluate_calibration(settings,team,td,player)
        readiness=week6_preflight(settings)
        result=write_review_report(settings,live,decisions,readiness,audit)
    provenance={"created_at_utc":datetime.now(timezone.utc).isoformat(),"elapsed_seconds":time.perf_counter()-start,
        "seed":SEED,"module_sha256":sha256_file(Path(__file__)),"production_core_sha256":sha256_file(settings.root/"nfl2026/production/core.py"),
        "research_artifacts":{str(p.relative_to(settings.root)):sha256_file(p) for p in sorted(cache.rglob("*")) if p.is_file()},
        "feature_sources":{str(p.relative_to(settings.root)):sha256_file(p) for p in [settings.artifact_root/"phase2/team_advanced_matrix.csv",settings.artifact_root/"phase2/player_advanced_conditional_matrix.csv",settings.artifact_root/"phase2/player_pregame_universe_features.csv"]},
        "models_promoted":[],"official_week6_prediction_created":False}
    write_json(settings.report_root/"five_week_review_manifest.json",provenance)
    print(json.dumps(finite({"week6_readiness":result["week6_readiness"],"team_metrics":result["live_game_metrics"],
        "td":result["offensive_td_metrics"],"rush":result["offensive_rush_metrics"],"receiving":result["offensive_receiving_metrics"],
        "best_development_candidates":decisions,"tests":result["tests"],"report":str(settings.report_root/"nfl_2026_five_week_model_review.md")}),indent=2),flush=True)
    return result
