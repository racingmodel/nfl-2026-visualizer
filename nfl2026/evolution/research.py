"""Small chronological challengers reusing archived incumbent forecasts."""
import json
import joblib
import numpy as np
import pandas as pd
from scipy.special import ndtr
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits
from nfl2026.phase7.common import json_write


def normalize_archived_scores(frame):
    """Partial bundles retain canonical names even if old convenience aliases are null."""
    d=frame.copy()
    for alias,canonical in [('pred_home_score','home_expected_points'),('pred_away_score','away_expected_points'),('actual_home_score','home_score'),('actual_away_score','away_score')]:
        if canonical not in d:continue
        if alias not in d:d[alias]=d[canonical]
        both=d[alias].notna()&d[canonical].notna()
        if not np.allclose(d.loc[both,alias],d.loc[both,canonical],atol=1e-8):raise ValueError('Conflicting archived score definitions: '+alias)
        d[alias]=d[alias].fillna(d[canonical])
    if d[['pred_home_score','pred_away_score','actual_home_score','actual_away_score']].isna().any().any():raise ValueError('Incomplete archived score population')
    return d


def metrics(g):
    he=g.pred_home_score-g.actual_home_score;ae=g.pred_away_score-g.actual_away_score
    p=g.home_win_probability.clip(1e-6,1-1e-6);non_tie=g.actual_home_score.ne(g.actual_away_score)&p.notna();y=g.actual_home_score.gt(g.actual_away_score)
    return dict(games=len(g),team_mae=pd.concat([he,ae]).abs().mean(),margin_mae=(he-ae).abs().mean(),total_mae=(he+ae).abs().mean(),
        home_bias=he.mean(),away_bias=ae.mean(),winner_accuracy=(p[non_tie].gt(.5)==y[non_tie]).mean(),brier=((p[non_tie]-y[non_tie])**2).mean(),
        log_loss=-(y[non_tie]*np.log(p[non_tie])+(1-y[non_tie])*np.log(1-p[non_tie])).mean(),probability_bias=(p[non_tie]-y[non_tie]).mean())


def team_form_rows(games):
    """Opponent adjustments use what was known before the *past* matchup."""
    d=games.copy();d['time']=pd.to_datetime(d.kickoff_utc,utc=True);d=d.sort_values(['time','game_id'])
    history={};rows=[]
    for time,simultaneous in d.groupby('time',sort=True):
        pending=[]
        for r in simultaneous.itertuples():
            value={'game_id':r.game_id,'season':r.season,'week':r.week,'kickoff_utc':r.kickoff_utc}
            for side,other in [('home','away'),('away','home')]:
                team=getattr(r,side+'_team');opponent=getattr(r,other+'_team');past=history.get(team,[]);defense=history.get(opponent,[])
                for label,window in [('long',8),('last3',3)]:
                    selected=past[-window:];value[f'{side}_{label}_off']=np.mean([p[0] for p in selected]) if selected else np.nan
                    value[f'{side}_{label}_def']=np.mean([p[1] for p in selected]) if selected else np.nan
                    value[f'{side}_{label}_adj']=np.mean([p[2] for p in selected]) if selected else np.nan
                for half_life in [2,4,8]:
                    weights=np.exp2(-np.arange(len(past[-16:]))[::-1]/half_life)
                    value[f'{side}_ew{half_life}']=np.average([p[0]-p[1] for p in past[-16:]],weights=weights) if len(weights) else np.nan
                scored=getattr(r,side+'_score');allowed=getattr(r,other+'_score')
                if pd.notna(scored) and pd.notna(allowed):
                    opponent_allowed=np.mean([p[1] for p in defense[-8:]]) if defense else 22.0
                    pending.append((team,(float(scored),float(allowed),float(scored)-opponent_allowed,str(r.game_id))))
            rows.append(value)
        for team,observation in pending:history.setdefault(team,[]).append(observation)
    return pd.DataFrame(rows)


def evaluate_forms(root,out):
    games=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    from nfl2026.evolution.audit import RUN
    meta=json.loads((root/'predictions/2026/week_07'/RUN/'metadata.json').read_text())
    live_schedule=pd.read_csv(root/'locks/2026/week_07'/meta['lock_id']/'inputs/games.csv',low_memory=False)
    live_schedule=live_schedule[live_schedule.season.eq(2026)].copy()
    live_schedule['kickoff_utc']=pd.to_datetime(live_schedule.gameday+' '+live_schedule.gametime.fillna('13:00')).dt.tz_localize('America/New_York').dt.tz_convert('UTC')
    games=pd.concat([games[games.season.lt(2026)],live_schedule],ignore_index=True)
    games=games[games.game_type.eq('REG')&games.season.between(2022,2026)&(games.season.lt(2026)|games.week.le(7))].copy()
    # Future rows are covariates only; their outcomes are deliberately blanked.
    games.loc[games.season.eq(2026)&games.week.ge(7),['home_score','away_score']]=np.nan
    form=team_form_rows(games)
    old=pd.read_csv(root/'artifacts/backtests/phase2_team_predictions.csv');old=old[old.model.eq('extra_trees')&old.season.isin([2023,2024,2025])]
    live=normalize_archived_scores(pd.read_csv(root/'reports/rigorous_benchmark_week7_20261022/final/official_games.csv'))
    old=old[['game_id','season','week','pred_home_score','pred_away_score','actual_home_score','actual_away_score']]
    live=live[old.columns];data=pd.concat([old,live],ignore_index=True).merge(form,on=['game_id','season','week'],validate='one_to_one')
    data['home_win_probability']=ndtr((data.pred_home_score-data.pred_away_score)/14)
    rows=[];coefficients=[];contributions=[]
    configs={'recent_form':[f'{s}_{n}' for s in ['home','away'] for n in ['long_off','long_def','last3_off','last3_def','last3_adj']],
        **{f'ew_h{h}':[f'{s}_{n}' for s in ['home','away'] for n in ['long_off','long_def',f'ew{h}']] for h in [2,4,8]}}
    for season in [2024,2025,2026]:
        train=data[data.season.lt(season)];test=data[data.season.eq(season)].copy()
        baseline=test.assign(candidate='incumbent');rows.append(baseline)
        for name,cols in configs.items():
            result=test.copy()
            for side in ['home','away']:
                estimator=make_pipeline(SimpleImputer(strategy='median'),StandardScaler(),Ridge(alpha=100))
                estimator.fit(train[cols],train['actual_'+side+'_score']-train['pred_'+side+'_score'])
                result['pred_'+side+'_score']=np.clip(result['pred_'+side+'_score']+estimator.predict(test[cols]),0,60)
                original_error=(result['pred_'+side+'_score']-test['actual_'+side+'_score']).abs().mean()
                rng=np.random.default_rng(season)
                for feature in cols:
                    shuffled=test[cols].copy();shuffled[feature]=rng.permutation(shuffled[feature].to_numpy())
                    counter=np.clip(test['pred_'+side+'_score']+estimator.predict(shuffled),0,60)
                    contributions.append(dict(season=season,candidate=name,side=side,feature=feature,
                        holdout_permutation_mae_increase=float((counter-test['actual_'+side+'_score']).abs().mean()-original_error),causal_effect=False))
                for feature,weight in zip(cols,estimator[-1].coef_):coefficients.append(dict(validation_season=season,candidate=name,side=side,feature=feature,standardised_residual_coefficient=weight))
            result['home_win_probability']=ndtr((result.pred_home_score-result.pred_away_score)/14)
            result['candidate']=name;result['training_max_season']=season-1;rows.append(result)
    predictions=pd.concat(rows,ignore_index=True);predictions.to_csv(out/'team_form_challenger_predictions.csv',index=False)
    summary=[]
    for (season,name),b in predictions.groupby(['season','candidate']):
        summary.append(dict(season=season,candidate=name,period='ALL',**metrics(b)))
        if len(b[b.week.le(4)]):summary.append(dict(season=season,candidate=name,period='WEEKS_1_4',**metrics(b[b.week.le(4)])))
    pd.DataFrame(summary).to_csv(out/'team_form_metrics.csv',index=False);pd.DataFrame(coefficients).to_csv(out/'team_form_coefficients.csv',index=False)
    pd.DataFrame(contributions).to_csv(out/'team_form_feature_contributions.csv',index=False)
    predictions['probability_bin']=pd.cut(predictions.home_win_probability,np.linspace(0,1,6),include_lowest=True).astype(str)
    predictions['home_won']=predictions.actual_home_score.gt(predictions.actual_away_score)
    predictions[predictions.actual_home_score.ne(predictions.actual_away_score)].groupby(['season','candidate','probability_bin']).agg(games=('game_id','size'),mean_probability=('home_win_probability','mean'),observed_home_win_rate=('home_won','mean')).to_csv(out/'team_form_calibration_bins.csv')
    # Select decay on 2024 only; 2025/2026 remain explicit later verification.
    m=pd.DataFrame(summary);choice=m[m.season.eq(2024)&m.period.eq('ALL')&m.candidate.str.startswith('ew_')].sort_values('team_mae').iloc[0].candidate
    json_write(out/'team_form_decision.json',{'selected_decay_on_2024':choice,'verification_seasons':[2025,2026],
        'probabilities':'common fixed 14-point margin diagnostic scale, not newly certified probabilities',
        'production_registered':False,'QB_aware_and_combined':'separate sparse 2025 QB-change experiment; not silently treated as certified inputs',
        'historical_source_publication_provenance':'incomplete; kickoff chronology enforced, exact live feature replay unavailable for every earlier 2026 fixture'})
    return predictions


def resolve_qb_context(row):
    required=['source_state','injury_source_state','projected_qb_id','status']
    if any(not row.get(k) for k in required) or row['source_state']!='VALID' or row['injury_source_state']!='VALID':raise ValueError('QB source conflict or missing evidence')
    if row.get('team') and row.get('player_team') and row['team']!=row['player_team']:raise ValueError('QB team identity conflict')
    if row.get('known_at_utc') and row.get('cutoff_utc'):
        known=pd.Timestamp(row['known_at_utc']);cutoff=pd.Timestamp(row['cutoff_utc'])
        if known.tzinfo is None or cutoff.tzinfo is None or known>=cutoff:raise ValueError('QB evidence not pregame')
    if row['status'] not in ['EXPECTED','VALIDATED','CONFIRMED']:raise ValueError('QB starter disputed')
    health=str(row.get('injury_status','UNKNOWN')).upper()
    if health in ['IR','OUT','SUSPENDED','INACTIVE','PUP']:raise ValueError('Selected QB unavailable')
    if health in ['QUESTIONABLE','DOUBTFUL']:return 'LIMITED_OR_UNCERTAIN_NO_ADJUSTMENT'
    if health in ['UNKNOWN','']:return 'HEALTH_UNCERTAIN_NO_ADJUSTMENT'
    if row.get('starter_confirmed') is True:return 'CONFIRMED'
    if str(row.get('confidence','')).upper()=='HIGH':return 'EXPECTED_HIGH_CONFIDENCE'
    return 'EXPECTED_UNCERTAIN_NO_ADJUSTMENT'


def qb_changes(root,out):
    d=pd.read_csv(root/'reports/qb_replacement_backtest.csv')
    volume=pd.read_csv(root/'artifacts/phase2/team_advanced_matrix.csv',usecols=['game_id','home_pregame_plays','home_pregame_pass_rate','away_pregame_plays','away_pregame_pass_rate'])
    d=d.merge(volume,on='game_id',validate='one_to_one')
    long=[]
    for side in ['home','away']:
        b=d.copy();b['side']=side;b['changed']=b[side+'_starter_change'].astype(bool)
        b['pregame_dropback_proxy']=b[side+'_pregame_plays']*b[side+'_pregame_pass_rate']
        b['quality_points_proxy']=b[side+'_incoming_minus_prior_starter_quality'].fillna(0)*b.pregame_dropback_proxy
        b['residual']=b['actual_'+side+'_score']-b['pred_'+side+'_score'];long.append(b)
    data=pd.concat(long,ignore_index=True);train=data[data.week.le(8)];test=data[data.week.gt(8)].copy()
    estimator=Ridge(alpha=200,fit_intercept=False);estimator.fit(train[['quality_points_proxy']],train.residual)
    test['candidate_adjustment']=estimator.predict(test[['quality_points_proxy']]);test['candidate_error']=test.candidate_adjustment-test.residual
    test.to_csv(out/'qb_change_calibration_rows.csv',index=False)
    result=[]
    for label,b in [('ALL',test),('INFERRED_QB_CHANGES',test[test.changed])]:
        result.append(dict(population=label,rows=len(b),incumbent_mae=b.residual.abs().mean(),candidate_mae=b.candidate_error.abs().mean(),incumbent_bias=-b.residual.mean(),candidate_bias=b.candidate_error.mean()))
    pd.DataFrame(result).to_csv(out/'qb_change_metrics.csv',index=False)
    json_write(out/'qb_availability_component_v1.json',{'version':'qb-quality-residual-v1','coefficient':float(estimator.coef_[0]),'quality_prior_dropbacks':200,
        'volume_proxy':'strict pregame rolling plays times pass rate; not a certified dropback forecast','volume_proxy_is_not_certified_attempt_forecast':True,'training':'2025 weeks 1-8 inferred starter states; no confirmed-OUT labels',
        'validation':'2025 weeks 9-18; small inferred-change subgroup','confirmed_OUT_matched_bookmaker_sample':0,
        'bookmaker_causality':'NOT_ESTABLISHED; no paired contemporaneous bookmaker/confirmed-OUT historical sample',
        'production_ready':False,'automatic_point_penalty':False,'activation':'research component only; identity/status conflict raises; uncertain/limited status gives no adjustment'})


def passing_baseline(root,out):
    from nfl2026.phase7.passing import lag_passing
    oos=pd.read_csv(root/'artifacts/research/phase7/qb_passing_oos.csv');history=pd.read_csv(root/'artifacts/research/phase7/qb_passing_history.csv')
    base=pd.read_csv(root/'reports/certification_week7_20261022/passing_existing_model_starter_rows.csv')
    # Saved as-of QB1 labels and exact prior-game lags are reused, not actual-start selection.
    d=base[base.expected_starter_depth].copy();d['baseline']=d.lag_baseline
    d['decomposed']=d.lag_pass_attempts*d.lag_passing_yards.div(d.lag_pass_attempts.replace(0,np.nan))
    # Recent passing attempts times a shrunk individual yards/attempt prior.
    mean_yards=history[history.season.lt(2025)].passing_yards.sum()/history[history.season.lt(2025)].pass_attempts.sum()
    d['shrunk_attempts_ypa']=d.lag_pass_attempts.fillna(30)*((d.lag_passing_yards.fillna(0)*d.passing_games_before.clip(upper=8)+50*mean_yards)/(d.lag_pass_attempts.fillna(0)*d.passing_games_before.clip(upper=8)+50))
    history['time']=pd.to_datetime(history.kickoff_utc,utc=True)
    team_history=history.groupby(['team','game_id','time'],as_index=False).passing_yards.sum()
    team_means=[];position_means=[];changes=[]
    for r in d.itertuples():
        stop=pd.Timestamp(r.cutoff_utc);past=history[history.time.lt(stop)];team=past[past.team.eq(r.team)].sort_values('time')
        team_means.append(team_history[team_history.team.eq(r.team)&team_history.time.lt(stop)].sort_values('time').tail(8).passing_yards.mean())
        position_means.append(past[past.pass_attempts.ge(10)].passing_yards.mean())
        last=team[team.time.eq(team.time.max())].sort_values('pass_attempts',ascending=False)
        changes.append(bool(len(last) and last.player_id.iloc[0]!=r.player_id))
    d['team_lag_baseline']=team_means;d['position_baseline']=position_means;d['inferred_qb_change']=changes
    records=[];intervals=[]
    for label,field in [('lag_baseline','baseline'),('historical_research','pred_passing_yards'),('attempts_x_shrunk_YPA','shrunk_attempts_ypa'),('team_prior8','team_lag_baseline'),('position_past_mean','position_baseline')]:
        for period,b in [('ALL_2025',d),('WEEKS_9_18',d[d.week.gt(8)])]:
            error=b[field]-b.passing_yards
            records.append(dict(candidate=label,period=period,rows=len(b),mae=error.abs().mean(),bias=error.mean(),rmse=np.sqrt((error**2).mean())))
        for week in range(9,19):
            past=d[d.week.lt(week)];test=d[d.week.eq(week)].copy()
            lo,hi=np.quantile(past.passing_yards-past[field],[.1,.9])
            test['p10']=(test[field]+lo).clip(lower=0);test['p90']=(test[field]+hi).clip(lower=0);test['candidate']=label
            test['covered80']=test.passing_yards.between(test.p10,test.p90);test['interval_training_max_week']=week-1;intervals.append(test)
    pd.DataFrame(records).to_csv(out/'passing_baseline_metrics.csv',index=False)
    interval=pd.concat(intervals);interval.to_csv(out/'passing_forward_starter_intervals.csv',index=False)
    interval.groupby('candidate').agg(rows=('covered80','size'),coverage80=('covered80','mean')).to_csv(out/'passing_baseline_calibration.csv')
    robustness=[]
    for name,field in [('lag_baseline','baseline'),('historical_research','pred_passing_yards'),('attempts_x_shrunk_YPA','shrunk_attempts_ypa')]:
        for label,b in [('INFERRED_CHANGE',d[d.inferred_qb_change]),('UNCHANGED',d[~d.inferred_qb_change]),('PRIOR_GAMES_LT8',d[d.passing_games_before.lt(8)])]:
            e=b[field]-b.passing_yards;robustness.append(dict(candidate=name,population=label,rows=len(b),mae=e.abs().mean(),bias=e.mean()))
    pd.DataFrame(robustness).to_csv(out/'passing_change_robustness.csv',index=False)
    rid='phase3_5-production-20261021T210639Z-e0207002';run=root/'predictions/2026/week_07'/rid
    qb=pd.read_csv(run/'qb_status.csv');games=pd.read_csv(run/'games.csv');qb=qb.merge(games[['game_id','kickoff_utc']],on='game_id',validate='many_to_one')
    idfield='qb_player_id' if 'qb_player_id' in qb else 'projected_qb_id';qb['player_id']=qb[idfield];qb['game_id']=qb.game_id;qb['season']=2026;qb['week']=7
    # Completed Week 1-6 player passing labels are in canonical PBP, never Week 7 outcomes.
    from nfl2026.data.passing_contract import aggregate_individual_passing
    import pyarrow.parquet as pq
    path=root/'NFL Fantasy/2026 Stats/pbp_2026.parquet';names=pq.ParquetFile(path).schema.names
    cols=['game_id','play_id','posteam','passer_player_id','passing_yards','pass_touchdown','pass_attempt','sack','two_point_attempt','play_type','complete_pass','week','season_type']
    pbp=pq.read_table(path,columns=cols).to_pandas();pbp=pbp[pbp.week.le(6)&pbp.season_type.eq('REG')]
    live=aggregate_individual_passing(pbp)
    from nfl2026.evolution.audit import RUN
    meta=json.loads((run/'metadata.json').read_text());schedule=pd.read_csv(root/'locks/2026/week_07'/meta['lock_id']/'inputs/games.csv',low_memory=False)
    schedule=schedule[schedule.season.eq(2026)].copy()
    schedule['kickoff_utc']=pd.to_datetime(schedule.gameday+' '+schedule.gametime.fillna('13:00')).dt.tz_localize('America/New_York').dt.tz_convert('UTC')
    schedule=schedule[['game_id','kickoff_utc']]
    live=live.merge(schedule,on='game_id',validate='many_to_one');live['season']=2026
    recent=lag_passing(qb,pd.concat([history,live],ignore_index=True))
    recent['model_only_research_passing_yards']=recent.lag_passing_yards
    players=pd.read_csv(run/'players.csv');team_receiving=players.groupby(['game_id','team']).expected_receiving_yards.sum()
    recent['frozen_team_receiving_yards']=[team_receiving.loc[(r.game_id,r.team)] for r in recent.itertuples()]
    recent['passing_minus_receiving']=recent.model_only_research_passing_yards-recent.frozen_team_receiving_yards
    lo,hi=np.quantile(d.passing_yards-d.baseline,[.1,.9])
    recent['research_p10']=(recent.model_only_research_passing_yards+lo).clip(lower=0);recent['research_p90']=(recent.model_only_research_passing_yards+hi).clip(lower=0)
    recent['certification']='BLOCKED; RESEARCH ONLY; NOT PUBLIC';recent.to_csv(out/'week7_passing_research_only.csv',index=False)
    json_write(out/'starter_passing_baseline_v1.json',{'version':'strict-prior8-starter-passing-v1','prediction_rule':'prior eight personal games with passing attempts; no actual-start selection',
        'training_required':False,'zero_history_policy':'uncertain/missing; no invented league-average forecast in current prospective output',
        'certification':'BLOCKED','blockers':['historical active/injury and feature observation timestamps incomplete','starter-only uncertainty validation limited to dated 2025 subset','complete live replay unavailable','passing/receiving coherence gaps remain; no silent rescaling','no matched passing market thresholds'],
        'official_passing_tab':'NOT_CERTIFIED','production_registered':False,'research_rows':len(recent),
        'interval_rule':'2025 dated-QB1 residual quantiles; research-only, no subgroup certification',
        'feature_support':'personal prior8 attempts/yards; opponent, pressure, weather, lineup and game script not yet validated incremental inputs',
        'runtime_required_features':['player_id','team','fixture kickoff','prior-game passing attempts/yards with source known_at before cutoff'],
        'coherence_max_absolute_gap':float(recent.passing_minus_receiving.abs().max())})


def matched_combined(root,out):
    """All five candidates on identical 2025 week 9-18 QB-evidence fixtures."""
    forms=pd.read_csv(out/'team_form_challenger_predictions.csv')
    q=pd.read_csv(out/'qb_change_calibration_rows.csv')
    piv=q.pivot(index='game_id',columns='side',values='candidate_adjustment')
    rows=[]
    for source,name,adjust in [('incumbent','incumbent',False),('recent_form','recent_form',False),('ew_h8','ew_h8',False),('incumbent','QB_aware',True),('recent_form','combined',True)]:
        b=forms[forms.season.eq(2025)&forms.week.gt(8)&forms.candidate.eq(source)&forms.game_id.isin(piv.index)].copy()
        if adjust:
            for side in ['home','away']:b['pred_'+side+'_score']+=b.game_id.map(piv[side])
        b['candidate']=name;b['home_win_probability']=ndtr((b.pred_home_score-b.pred_away_score)/14);rows.append(b)
    d=pd.concat(rows);d.to_csv(out/'matched_five_challenger_predictions.csv',index=False)
    pd.DataFrame([dict(candidate=k,**metrics(g)) for k,g in d.groupby('candidate')]).to_csv(out/'matched_five_challenger_metrics.csv',index=False)
    # Paired game resampling measures score-error uncertainty, not causal injury impact.
    incumbent=d[d.candidate.eq('incumbent')].set_index('game_id');records=[];rng=np.random.default_rng(20261022)
    for name,g in d.groupby('candidate'):
        b=g.set_index('game_id').loc[incumbent.index]
        delta=((b.pred_home_score-b.actual_home_score).abs()+(b.pred_away_score-b.actual_away_score).abs()
            -(incumbent.pred_home_score-incumbent.actual_home_score).abs()-(incumbent.pred_away_score-incumbent.actual_away_score).abs()).to_numpy()/2
        means=delta[rng.integers(len(delta),size=(2000,len(delta)))].mean(axis=1)
        records.append(dict(candidate=name,games=len(delta),paired_team_mae_change=float(delta.mean()),ci95_low=float(np.quantile(means,.025)),ci95_high=float(np.quantile(means,.975))))
    pd.DataFrame(records).to_csv(out/'matched_challenger_uncertainty.csv',index=False)


def bookmaker_reference(root,out):
    """Reuse sealed cohorts; never relabel stale/later prices contemporaneous."""
    source=root/'reports/rigorous_benchmark_week7_20261022/final/matched_fixture_consensus.csv'
    archive=normalize_archived_scores(pd.read_csv(source));forms=pd.read_csv(out/'team_form_challenger_predictions.csv');rows=[];summary=[]
    for cohort,books in archive.groupby('cohort'):
        books=books.drop_duplicates('game_id');matched=forms[forms.season.eq(2026)&forms.game_id.isin(books.game_id)].copy()
        for name,g in matched.groupby('candidate'):summary.append(dict(cohort=cohort,candidate=name,probability_basis='common 14-point diagnostic margin scale',**metrics(g)))
        b=books.copy();b['pred_home_score']=(b.market_total-b.home_handicap)/2;b['pred_away_score']=(b.market_total+b.home_handicap)/2;b['home_win_probability']=b.market_home_probability
        summary.append(dict(cohort=cohort,candidate='bookmaker_consensus',probability_basis='paired bookmaker probabilities where present',**metrics(b)))
        summary.append(dict(cohort=cohort,candidate='official_frozen_probabilities',probability_basis='actual archived original probabilities',**metrics(books)))
        matched['cohort']=cohort;rows.append(matched)
    pd.DataFrame(summary).to_csv(out/'bookmaker_reference_metrics.csv',index=False)
    json_write(out/'bookmaker_reference_limitations.json',{'source_path':str(source.relative_to(root)),
        'same_time_fresh_cohort_games':int(archive[~archive.cohort.isin(['FORECAST_TIME_STALE','LATER_PREGAME'])].game_id.nunique()),
        'cohort_counts':archive.groupby('cohort').game_id.nunique().to_dict(),
        'interpretation':'stale forecast-time and later pregame benchmarks reported separately; neither proves a contemporaneous QB-injury effect',
        'confirmed_OUT_timestamp_matched_sample':0,'no_new_provider_requests':True})
