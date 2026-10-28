"""Fold-fitted corrections and passing research; production registration forbidden."""
from datetime import datetime, timezone
import json

import joblib
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import norm
from scipy.special import logit
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from nfl2026.research.rigorous_review import (save, cluster_ci, assert_chronology, SEED)
from nfl2026.phase7.common import json_write
from nfl2026.five_week_review import finite
from nfl2026.utilities.io import sha256_file

KEYS=['game_id','player_id','team','season','week']
RUN='phase3_5-production-20261021T210639Z-e0207002'


def fit_correction(prediction, actual):
    """Robust affine correction; only caller's prior fold may enter the fit."""
    x,y=np.asarray(prediction,float),np.asarray(actual,float)
    valid=np.isfinite(x)&np.isfinite(y)
    if valid.sum()<30:raise ValueError('Insufficient prior calibration population')
    result=minimize(lambda a:np.mean(np.abs(np.maximum(0,a[0]+a[1]*x[valid])-y[valid])),
        [0.,1.],method='Powell',bounds=[(-30,30),(0,2)],options={'maxiter':400})
    if not result.success:raise ValueError('Bias correction did not converge')
    return result.x


def apply_correction(prediction, coefficients):
    if len(coefficients)!=2 or not np.isfinite(coefficients).all():raise ValueError('Invalid correction')
    return np.maximum(0,coefficients[0]+coefficients[1]*np.asarray(prediction,float))


def fit_mean_correction(prediction, actual):
    """Squared-error fit targets a conditional mean, unlike the MAE median fit."""
    x,y=np.asarray(prediction,float),np.asarray(actual,float)
    valid=np.isfinite(x)&np.isfinite(y)
    if valid.sum()<30:raise ValueError('Insufficient prior mean calibration population')
    result=minimize(lambda a:np.mean((np.maximum(0,a[0]+a[1]*x[valid])-y[valid])**2),
        [0.,1.],method='Powell',bounds=[(-30,30),(0,2)],options={'maxiter':400})
    if not result.success:raise ValueError('Mean correction did not converge')
    return result.x


def evaluate_pair(output, area, frame, actual, baseline, candidate):
    rows=[]
    for period,b in [('ALL',frame),*frame.groupby('season')]:
        x=(b[baseline]-b[actual]).abs();y=(b[candidate]-b[actual]).abs()
        delta=y-x;ci=cluster_ci(b,delta,seasons=True)
        rows.append({'area':area,'period':str(period),'rows':len(b),'games':b.game_id.nunique(),
            'folds':b.season.nunique(),'champion_mae':x.mean(),'candidate_mae':y.mean(),'absolute_change':delta.mean(),
            'percent_change':100*delta.mean()/x.mean(),'difference_ci_low':ci[0],'difference_ci_high':ci[1],
            'champion_bias':(b[baseline]-b[actual]).mean(),'candidate_bias':(b[candidate]-b[actual]).mean(),
            'provenance_certified':False,'promoted':False})
    save(output,area+'_candidate_metrics',pd.DataFrame(rows))
    return rows


def position_corrections(root,output):
    d=pd.read_csv(root/'artifacts/research/five_week_review/player_production_semantics_replay.csv')
    assert_chronology(d)
    if d.duplicated(KEYS).any():raise ValueError('Duplicate incumbent research rows')
    parts=[];metrics=[];coefficients={}
    for area,position,target in [('qb_rushing','QB','rushing_yards'),('te_receiving','TE','receiving_yards')]:
        b=d[d.position.eq(position)].copy();pieces=[]
        for season in [2024,2025]:
            train=b[b.season.lt(season)];test=b[b.season.eq(season)].copy()
            if pd.to_datetime(train.kickoff_utc,utc=True).max()>=pd.to_datetime(test.kickoff_utc,utc=True).min():raise ValueError('Correction cutoff leakage')
            coef=fit_correction(train['pred_'+target],train['actual_'+target])
            test['candidate']=apply_correction(test['pred_'+target],coef)
            mean_coef=fit_mean_correction(train['pred_'+target],train['actual_'+target])
            test['mean_candidate']=apply_correction(test['pred_'+target],mean_coef)
            test['correction_intercept']=coef[0];test['correction_scale']=coef[1]
            test['correction_training_max']=pd.to_datetime(train.kickoff_utc,utc=True).max().isoformat()
            pieces.append(test)
        scored=pd.concat(pieces,ignore_index=True)
        metrics.extend(evaluate_pair(output,area,scored,'actual_'+target,'pred_'+target,'candidate'))
        metrics.extend(evaluate_pair(output,area+'_mean',scored,'actual_'+target,'pred_'+target,'mean_candidate'))
        save(output,area+'_chronological_predictions',scored)
        coef=fit_mean_correction(b['pred_'+target],b['actual_'+target])
        coefficients[area]={'coefficients':coef.tolist(),'position':position,'target':target,
            'max_training_kickoff_utc':pd.to_datetime(b.kickoff_utc,utc=True).max().isoformat(),
            'training_seasons':[2023,2024,2025],'source_sha256':sha256_file(root/'artifacts/research/five_week_review/player_production_semantics_replay.csv'),
            'qualification':'Mean-targeted MSE affine correction; PIT-semantics replay, NOT exact immutable live champion replay; historical roster publication unproved'}
        parts.append(scored.assign(area=area))
    path=output/'models/position-affine-mean-v1.joblib';path.parent.mkdir(exist_ok=True)
    joblib.dump(coefficients,path)
    json_write(path.with_suffix('.json'),{'status':'EXPERIMENTAL_NOT_REGISTERED','sha256':sha256_file(path),'models':coefficients,
        'selection':'Predeclared affine MAE and mean-targeted MSE fits; prospective output uses only the mean fit. 2024 development, 2025 confirmation. Outcomes inspected by earlier research; not a new untouched external test.',
        'promotion_gate':'Paired upper95CI<0; bias noninferiority; >=2 season improvement; certified pregame inputs; exact live replay; independent prospective evaluation; all required'})
    return coefficients,metrics


def team_research(root,output):
    d=pd.read_csv(root/'artifacts/research/five_week_review/team_candidates.csv')
    assert_chronology(d)
    base=d[d.model.eq('incumbent_phase2_v2')]
    candidate=d[d.model.eq('pooled_extra_trees')&d.variant.eq('ewm8')]
    b=base.merge(candidate,on=['game_id','season','week'],suffixes=('_base','_candidate'),validate='one_to_one')
    b['game_id']=b.game_id;b['actual']=(b.actual_home_score_base-b.actual_away_score_base)
    b['baseline']=b.pred_home_score_base-b.pred_away_score_base
    b['candidate']=b.pred_home_score_candidate-b.pred_away_score_candidate
    metrics=evaluate_pair(output,'team_margin_archived',b,'actual','baseline','candidate')
    b['actual_score_error']=(abs(b.pred_home_score_base-b.actual_home_score_base)+abs(b.pred_away_score_base-b.actual_away_score_base))/2
    b['candidate_score_error']=(abs(b.pred_home_score_candidate-b.actual_home_score_base)+abs(b.pred_away_score_candidate-b.actual_away_score_base))/2
    for season,s in b.groupby('season'):
        delta=s.candidate_score_error-s.actual_score_error;ci=cluster_ci(s,delta)
        metrics.append({'area':'team_scores_archived','period':str(season),'rows':len(s),'folds':1,
            'champion_mae':s.actual_score_error.mean(),'candidate_mae':s.candidate_score_error.mean(),
            'absolute_change':delta.mean(),'percent_change':100*delta.mean()/s.actual_score_error.mean(),
            'difference_ci_low':ci[0],'difference_ci_high':ci[1],'promoted':False,'provenance_certified':False})
    # Probability calibration is fitted only to prior chronological OOS forecasts.
    b['p']=norm.cdf(b.baseline/np.sqrt(156));b['outcome']=b.actual.gt(0).astype(int)
    b=b[b.actual.ne(0)]
    predictions=[];prob_metrics=[]
    for season in [2024,2025]:
        train=b[b.season.lt(season)];test=b[b.season.eq(season)].copy()
        model=LogisticRegression(C=1,random_state=SEED).fit(logit(train.p.to_numpy()).reshape(-1,1),train.outcome)
        test['candidate_probability']=model.predict_proba(logit(test.p.to_numpy()).reshape(-1,1))[:,1]
        predictions.append(test[['game_id','season','week','p','outcome','candidate_probability']])
        for metric in ['brier','log_loss']:
            p=test.p.to_numpy();c=test.candidate_probability.to_numpy();y=test.outcome.to_numpy()
            x=(p-y)**2 if metric=='brier' else -(y*np.log(p)+(1-y)*np.log1p(-p))
            z=(c-y)**2 if metric=='brier' else -(y*np.log(c)+(1-y)*np.log1p(-c))
            ci=cluster_ci(test,z-x)
            prob_metrics.append({'area':'win_'+metric,'period':str(season),'rows':len(test),'folds':1,
                'champion_metric':x.mean(),'candidate_metric':z.mean(),'absolute_change':(z-x).mean(),
                'percent_change':100*(z-x).mean()/x.mean(),'difference_ci_low':ci[0],'difference_ci_high':ci[1],
                'qualification':'Analytic replay win probability; not saved live simulation probability','promoted':False})
    save(output,'win_calibration_chronological',pd.concat(predictions));save(output,'win_calibration_metrics',pd.DataFrame(prob_metrics))
    save(output,'team_challenger_metrics',pd.DataFrame(metrics))
    return metrics+prob_metrics


def passing_features(candidates,history):
    """No target-game outcomes/nominal starter labels enter these predictors."""
    out=candidates.copy().reset_index(drop=True)
    history=history.copy();history['time']=pd.to_datetime(history.kickoff_utc,utc=True)
    indexed={pid:g.sort_values('time') for pid,g in history.groupby('player_id')}
    columns=['attempts_4','attempts_8','yards_4','yards_8','ypa_8','passing_history_count','team_attempts_6','opponent_yards_6','home']
    for c in columns:out[c]=np.nan
    teamhistory=history.groupby(['game_id','team','kickoff_utc'])[['pass_attempts','passing_yards']].sum().reset_index()
    teamhistory['time']=pd.to_datetime(teamhistory.kickoff_utc,utc=True)
    for i,row in out.iterrows():
        cutoff=pd.Timestamp(row.kickoff_utc)
        prior=indexed.get(row.player_id)
        if prior is not None:
            prior=prior[prior.time.lt(cutoff)&prior.pass_attempts.gt(0)]
            out.loc[i,'passing_history_count']=len(prior)
            if len(prior):
                last=prior.tail(8)
                out.loc[i,['attempts_4','attempts_8','yards_4','yards_8','ypa_8']]=[
                    prior.tail(4).pass_attempts.mean(),last.pass_attempts.mean(),prior.tail(4).passing_yards.mean(),last.passing_yards.mean(),
                    last.passing_yards.sum()/last.pass_attempts.sum()]
        own=teamhistory[teamhistory.team.eq(row.team)&teamhistory.time.lt(cutoff)].sort_values('time').tail(6)
        if len(own):out.loc[i,'team_attempts_6']=own.pass_attempts.mean()
        if 'opponent' in out:
            opponent_games=set(out.loc[out.team.eq(row.opponent)&pd.to_datetime(out.kickoff_utc,utc=True).lt(cutoff),'game_id'])
            defense=teamhistory[teamhistory.game_id.isin(opponent_games)&teamhistory.team.ne(row.opponent)].sort_values('time').tail(6)
            if len(defense):out.loc[i,'opponent_yards_6']=defense.passing_yards.mean()
        out.loc[i,'home']=float(str(row.game_id).split('_')[-1]==str(row.team))
    return out,columns


def eligible_research_qbs(qbs):
    """Research is also fail closed on ambiguous identities or critical availability."""
    required={'game_id','team','qb_player_id','status','source_state','injury_status'}
    if not required.issubset(qbs) or qbs.duplicated(['game_id','team']).any():
        raise ValueError('QB evidence missing or conflicting')
    valid=(qbs.qb_player_id.notna() & qbs.qb_player_id.astype(str).str.match(r'^00-\d+$')
        & qbs.status.isin(['EXPECTED','CONFIRMED']) & qbs.source_state.eq('VALID')
        & qbs.injury_status.fillna('UNKNOWN').str.upper().isin(['UNKNOWN','ACTIVE','HEALTHY','NONE']))
    return qbs[valid].copy()


def passing_research(root,output):
    old=pd.read_csv(root/'artifacts/research/phase7/qb_passing_oos.csv');assert_chronology(old)
    history=pd.read_csv(root/'artifacts/research/phase7/qb_passing_history.csv')
    schedule=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    # Exact team/opponent comes from schedule identity, never inferred by outcome.
    mapping=pd.concat([schedule[['game_id','home_team','away_team']].rename(columns={'home_team':'team','away_team':'opponent'}),
        schedule[['game_id','home_team','away_team']].rename(columns={'away_team':'team','home_team':'opponent'})])
    old=old.merge(mapping,on=['game_id','team'],validate='many_to_one')
    data,features=passing_features(old,history)
    save(output,'passing_feature_coverage',pd.DataFrame([{'feature':f,'rows':len(data),'missing':int(data[f].isna().sum()),
        'policy':'Strictly prior kickoff; training-fold imputer; no role publication certification'} for f in features]))
    predictions=[];last_models={};metrics=[]
    with threadpool_limits(2):
        for season in [2024,2025]:
            train=data[data.season.lt(season)];test=data[data.season.eq(season)].copy()
            if pd.to_datetime(train.kickoff_utc,utc=True).max()>=pd.to_datetime(test.kickoff_utc,utc=True).min():raise ValueError('Passing cutoff leakage')
            test['lag_baseline']=test.yards_8.fillna(train.passing_yards.median())
            definitions={'passing_lag_et_v2':ExtraTreesRegressor(n_estimators=120,min_samples_leaf=12,max_depth=8,random_state=SEED,n_jobs=2),
                'passing_lag_hgb_v2':HistGradientBoostingRegressor(max_iter=120,max_leaf_nodes=12,l2_regularization=10,random_state=SEED)}
            for name,estimator in definitions.items():
                model=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),estimator)
                model.fit(train[features],train.passing_yards)
                test[name]=np.maximum(0,model.predict(test[features]));last_models[name]=model
            test['research_fold_training_max']=pd.to_datetime(train.kickoff_utc,utc=True).max().isoformat()
            predictions.append(test)
    scored=pd.concat(predictions,ignore_index=True)
    for name in ['lag_baseline','passing_lag_et_v2','passing_lag_hgb_v2']:
        metrics.extend(evaluate_pair(output,name,scored,'passing_yards','pred_passing_yards',name))
    save(output,'passing_chronological_predictions',scored)
    # Keep all strata; actual workload is a retrospective diagnostic, never X or eligibility.
    segments=[]
    for key,values in [('actual_workload',pd.cut(scored.pass_attempts,[-1,0,15,30,45,np.inf])),
                       ('projection_band',pd.cut(scored.passing_lag_et_v2,[-1,100,200,250,300,np.inf])),('team',scored.team)]:
        for label,b in scored.groupby(values,observed=True):
            for name in ['pred_passing_yards','lag_baseline','passing_lag_et_v2','passing_lag_hgb_v2']:
                e=b[name]-b.passing_yards
                segments.append({'segment':key,'value':str(label),'model':name,'rows':len(b),'mae':e.abs().mean(),'bias':e.mean()})
    save(output,'passing_segments',pd.DataFrame(segments))
    # Final research ET is predeclared, not chosen by the confirmation score.
    with threadpool_limits(2):
        final=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True),ExtraTreesRegressor(
            n_estimators=120,min_samples_leaf=12,max_depth=8,random_state=SEED,n_jobs=2)).fit(data[features],data.passing_yards)
    path=output/'models/passing-lag-et-v2-through2025.joblib'
    joblib.dump({'model':final,'features':features},path)
    json_write(path.with_suffix('.json'),{'status':'EXPERIMENTAL_NOT_REGISTERED','sha256':sha256_file(path),
        'max_training_kickoff_utc':pd.to_datetime(data.kickoff_utc,utc=True).max().isoformat(),'training_seasons':[2023,2024,2025],
        'history_seasons':[2022,2023,2024,2025],'source_sha256':{str(p.relative_to(root)):sha256_file(p) for p in [root/'artifacts/research/phase7/qb_passing_oos.csv',root/'artifacts/research/phase7/qb_passing_history.csv']},
        'certification':'BLOCKED','gates':['Historical pregame starter/eligibility publication unproved',
            '2025 attempt labels disagree with weekly source (512/660); authoritative label contract unresolved',
            'Intervals and threshold probabilities lack independent calibration',
            'Primary MAE upper95 paired CI <0 and >=2 fold improvement required; bias cannot worsen materially',
            'Independent prospective expected-starter evaluation and full production contract required'],
        'unsupported_features':['timestamp-valid weather','receiver injury roles historically','pressure','neutral situation passing','completions not in this artifact'],
        'feature_semantics':'last4/8 personal pass games, last6 team volume/opponent allowance; all strictly before fixture; no actual starter selection'})
    return final,features,history,metrics


def relevance(frame):
    """Explainable workload ranking within each team; no yardage/odds threshold."""
    d=frame.sort_values(['game_id','team','expected_targets','player_id'],ascending=[True,True,False,True]).copy()
    if d.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate receiver identity')
    d['opportunity_rank']=d.groupby(['game_id','team']).expected_targets.rank(method='first',ascending=False)
    d['receiving_group']=np.where(d.opportunity_rank.le(2),'FEATURED','ALL_ELIGIBLE')
    if 'prior_targets' in d:
        d['target_growth']=d.expected_targets-d.prior_targets
        priorrank=d.groupby(['game_id','team']).prior_targets.rank(method='first',ascending=False)
        d.loc[d.opportunity_rank.le(2)&priorrank.gt(2)&d.target_growth.gt(0),'receiving_group']='EMERGING'
    return d


def wr_research(root,output,players):
    source=pd.read_csv(root/'artifacts/research/five_week_review/player_production_semantics_replay.csv')
    d=source[source.position.eq('WR')].copy().rename(columns={'pred_targets':'expected_targets'})
    d=relevance(d)
    metrics=[]
    for season,b in d.groupby('season'):
        # Operational high-opportunity diagnostic is >=5 actual targets; not a fitted selector.
        for label,chosen in [('ALL_NOMINAL',b),('TOP_TWO_TEAM_WORKLOAD',b[b.opportunity_rank.le(2)])]:
            high=b.actual_targets.ge(5);hit=chosen.actual_targets.ge(5)
            metrics.append({'season':season,'selector':label,'displayed':len(chosen),'all_receivers':len(b),
                'clutter_reduction':1-len(chosen)/len(b),'high_opportunity_coverage':hit.sum()/high.sum(),
                'weak_under2_targets_fraction':chosen.actual_targets.lt(2).mean(),
                'high_opportunity_precision':hit.mean(),'starter_publication_certified':False,
                'emerging_recall':'NOT_CERTIFIED; historical growth/role publication unavailable'})
    save(output,'wr_ranking_validation',pd.DataFrame(metrics));save(output,'wr_historical_ranking',d)
    p=players[players.position.eq('WR')].copy()
    save(output,'wr_live_display_diagnostics',p.assign(weak_projected_opportunity=p.expected_targets.lt(2)))
    return metrics


def prospective(root,output,coefficients,passing_model,features,history):
    run=root/'predictions/2026/week_07'/RUN
    meta=json.loads((run/'metadata.json').read_text());official=pd.read_csv(run/'players.csv');games=pd.read_csv(run/'games.csv')
    now=pd.Timestamp(datetime.now(timezone.utc))
    if not (output/'week7_independent_states.csv').exists():
        return {'status':'BLOCKED_NO_INDEPENDENT_CURRENT_STATES','official_unchanged':True}
    states=pd.read_csv(output/'week7_independent_states.csv')
    lookup=states.set_index('game_id')
    eligible=[r.game_id for r in games.itertuples() if r.game_id in lookup.index and
        lookup.loc[r.game_id,'eligibility_state']=='PRE_KICKOFF' and pd.Timestamp(r.kickoff_utc)>now and
        pd.Timestamp(lookup.loc[r.game_id,'kickoff_utc'])==pd.Timestamp(r.kickoff_utc)]
    if not eligible:return {'status':'BLOCKED_NO_PREGAME_FIXTURES','official_unchanged':True}
    out=official[official.game_id.isin(eligible)].copy()
    for area,field in [('qb_rushing','expected_rush_yards'),('te_receiving','expected_receiving_yards')]:
        model=coefficients[area];mask=out.position.eq(model['position'])
        out.loc[mask,field]=apply_correction(out.loc[mask,field],model['coefficients'])
    out['status']='PROSPECTIVE_RESEARCH_NOT_PRODUCTION';out['original_run_id']=RUN
    # This is a mean-only shadow: remove stale incumbent uncertainty fields.
    out=out[[c for c in out if not any(t in c for t in ['_low','_high','distribution','fair_odds'])]]
    save(output,'week7_position_challenger',out)
    # Passing history adds only completed Weeks1-6, never target-week PBP.
    import pyarrow.parquet as pq
    from nfl2026.phase7.passing import aggregate_passing
    path=root/'NFL Fantasy/2026 Stats/pbp_2026.parquet'
    columns=['game_id','play_id','posteam','passer_player_id','passing_yards','pass_touchdown','pass_attempt','sack','two_point_attempt','play_type','complete_pass','week']
    pbp=pq.read_table(path,columns=columns,filters=[('week','<',7)]).to_pandas()
    actual=aggregate_passing(pbp)
    schedule=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    actual=actual.merge(schedule[['game_id','kickoff_utc']],on='game_id',validate='many_to_one')
    actual['season']=2026
    if pd.to_datetime(actual.kickoff_utc,utc=True).max()>=now:raise ValueError('Future passing history')
    history=pd.concat([history,actual],ignore_index=True)
    qbs=pd.read_csv(root/f"locks/2026/week_07/{meta['lock_id']}/inputs/qb_status_2026_w07.csv")
    qbs=eligible_research_qbs(qbs)
    idfield=next(c for c in ['expected_qb_id','qb_player_id','player_id','qb_id'] if c in qbs)
    chosen=official[official.position.eq('QB')&official.game_id.isin(eligible)].merge(
        qbs[['team',idfield]].rename(columns={idfield:'player_id'}),on=['team','player_id'],validate='many_to_one')
    chosen=chosen.merge(games[['game_id','kickoff_utc']],on='game_id',validate='many_to_one')
    panel,_=passing_features(chosen,history)
    panel['research_expected_passing_yards']=np.maximum(0,passing_model.predict(panel[features]))
    panel['status']='MODEL_ONLY_RESEARCH_NOT_CERTIFIED';panel['bookmaker_passing_quote']=None
    save(output,'week7_passing_challenger',panel[['game_id','player_id','player_name','team','opponent','kickoff_utc','research_expected_passing_yards','status','bookmaker_passing_quote']])
    completed=pd.Timestamp(datetime.now(timezone.utc))
    if pd.to_datetime(panel.kickoff_utc,utc=True).le(completed).any():raise ValueError('Kickoff reached during research inference')
    lock=root/f"locks/2026/week_07/{meta['lock_id']}"
    inputs=pd.read_csv(lock/'inputs/player_pregame_universe_features.csv',low_memory=False)
    wr=official[official.position.eq('WR')&official.game_id.isin(eligible)].merge(
        inputs[['game_id','player_id','team','prior_targets']],on=['game_id','player_id','team'],validate='one_to_one')
    wr=relevance(wr);save(output,'week7_wr_discovery',wr)
    sources=[run/'metadata.json',run/'players.csv',run/'games.csv',lock/'data_lock.json',lock/'inputs/player_pregame_universe_features.csv',path,output/'week7_state_source.json',output/'week7_independent_states.csv']
    result={'status':'PROSPECTIVE_RESEARCH_ONLY','created_at_utc':completed.isoformat(),'original_run_id':RUN,
        'original_freeze_id':'freeze-20261021T210711Z-53d7df54','eligible_game_ids':eligible,'games':len(eligible),'players':len(out),'passing_qbs':len(panel),
        'certification':'BLOCKED','official_replacement':'NOT_PERMITTED_UNCERTIFIED_MODELS',
        'qualification':'Uses original frozen pregame roster and feature evidence; no claim of newly refreshed football sources. No intervals, value selections or official Passing tab.',
        'source_sha256':{str(p.relative_to(root)):sha256_file(p) for p in sources},
        'files':{str(p.relative_to(output)):sha256_file(p) for p in [*output.glob('week7_*challenger.csv'),*output.glob('models/*')]},'official_unchanged':True,
        'market_snapshot_used_as_fresh':False,'new_freeze':None,'promoted':False}
    json_write(output/'prospective_challenger_manifest.json',result)
    return result


def evaluate(root,output,players):
    coefficients,metrics=position_corrections(root,output)
    metrics.extend(team_research(root,output))
    final,features,history,passing=passing_research(root,output);metrics.extend(passing)
    wr=wr_research(root,output,players)
    save(output,'all_challenger_metrics',pd.DataFrame(metrics))
    shadow=prospective(root,output,coefficients,final,features,history)
    result={'all_promotions':False,'historical_role_provenance':'NOT_CERTIFIED','metrics':metrics,'wr':wr,'week7':shadow}
    json_write(output/'challenger_decisions.json',finite(result))
    return result
