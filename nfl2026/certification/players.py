"""Close precise player certification gaps without refitting archived models."""
from datetime import datetime, timezone
import json

import joblib
import numpy as np
import pandas as pd
from scipy.stats import norm
from threadpoolctl import threadpool_limits

from nfl2026.utilities.io import sha256_file
from nfl2026.phase7.common import json_write
from nfl2026.research.rigorous_review import cluster_ci
from nfl2026.research.review_challengers import apply_correction

KEYS=['game_id','player_id','team']


def asof_depth(raw,candidates,*,max_age_hours=36):
    """Use source-loaded snapshot timestamps, never week labels or actual starts.

    A depth role is narrower evidence than injury/active-roster eligibility.
    Parallel WR/TE slots are retained; tied ranks and conflicting QB1s fail closed.
    """
    required={'dt','team','gsis_id','pos_abb','pos_rank','pos_grp_id','pos_id','pos_slot'}
    if not required.issubset(raw):raise ValueError('Timestamped depth schema unavailable')
    d=raw.copy();d['dt']=pd.to_datetime(d.dt,utc=True,errors='coerce')
    if d.dt.isna().any():raise ValueError('Unknown depth observation time')
    d['pos_rank']=pd.to_numeric(d.pos_rank,errors='coerce')
    d=d.drop_duplicates(list(required))
    indexed={team:g for team,g in d.groupby('team')}
    snapshots={team:sorted(g.dt.unique()) for team,g in indexed.items()}
    rows=[]
    for (gid,team),b in candidates.groupby(['game_id','team'],sort=False):
        if b.cutoff_utc.nunique()!=1:raise ValueError('Conflicting forecast cutoff')
        cutoff=pd.Timestamp(b.cutoff_utc.iloc[0]);kickoff=pd.Timestamp(b.kickoff_utc.iloc[0])
        if cutoff.tzinfo is None or not cutoff<kickoff:raise ValueError('Depth cutoff must precede kickoff')
        available=[t for t in snapshots.get(team,[]) if t<=cutoff]
        snapshot=max(available) if available else None
        current=indexed[team][indexed[team].dt.eq(snapshot)].copy() if snapshot is not None else pd.DataFrame()
        fresh=snapshot is not None and cutoff-pd.Timestamp(snapshot)<=pd.Timedelta(hours=max_age_hours)
        leaders={};conflict=set()
        if fresh:
            for position,g in current[current.pos_abb.isin(['QB','RB','FB','WR','TE'])].groupby('pos_abb'):
                chosen=set()
                for _,slot in g.groupby(['pos_grp_id','pos_id','pos_slot'],dropna=False):
                    if slot.pos_rank.isna().any():conflict.add(position);continue
                    ids=set(slot.loc[slot.pos_rank.eq(slot.pos_rank.min()),'gsis_id'].dropna().astype(str))
                    if len(ids)!=1:conflict.add(position)
                    else:chosen.update(ids)
                if position=='QB' and len(chosen)!=1:conflict.add(position)
                leaders[position]=chosen
        for r in b.to_dict('records'):
            role=bool(fresh and r['position'] not in conflict and str(r['player_id']) in leaders.get(r['position'],set()))
            rows.append({**r,'depth_snapshot_utc':pd.Timestamp(snapshot).isoformat() if snapshot is not None else None,
                'depth_role_evidence':'CONFLICT' if r['position'] in conflict else 'TIMESTAMPED_PREGAME_DEPTH' if fresh else 'MISSING_OR_STALE',
                'expected_starter_depth':role,'eligibility_certified':False,
                'eligibility_gap':'Depth identifies role; independent active/injury/transaction state still required'})
    return pd.DataFrame(rows)


def passing_labels(pbp):
    """Individual NFL passing attempts, not dropbacks; exact error decomposition."""
    required={'game_id','play_id','posteam','passer_player_id','passing_yards','complete_pass','pass_attempt','sack','play_type','two_point_attempt','pass','pass_touchdown'}
    if not required.issubset(pbp) or pbp.duplicated(['game_id','play_id']).any():raise ValueError('Invalid passing PBP')
    p=pbp[pbp.passer_player_id.notna()&pbp.posteam.notna()].copy()
    legacy=p['pass'].fillna(0).gt(0)|p.pass_attempt.fillna(0).gt(0)
    no_play=legacy&p.play_type.eq('no_play')
    two_point=legacy&~no_play&p.two_point_attempt.eq(1)
    sack=legacy&~no_play&~two_point&p.sack.eq(1)
    valid=~p.play_type.eq('no_play')&p.two_point_attempt.ne(1)
    attempt=valid&p.pass_attempt.eq(1)&p.sack.ne(1)
    if p.loc[valid&p.complete_pass.eq(1),'passing_yards'].isna().any():raise ValueError('Unknown completed-pass yards')
    p['legacy_dropback_attempts']=legacy.astype(int)
    p['excluded_no_play']=no_play.astype(int);p['excluded_two_point']=two_point.astype(int);p['excluded_sack']=sack.astype(int)
    p['pass_attempts']=attempt.astype(int)
    p['remaining_flag_difference']=p.legacy_dropback_attempts-p.excluded_no_play-p.excluded_two_point-p.excluded_sack-p.pass_attempts
    p['passing_yards']=p.passing_yards.where(valid,0).fillna(0)
    p['completions']=(valid&p.complete_pass.eq(1)).astype(int)
    p['passing_tds']=p.pass_touchdown.where(valid,0).fillna(0)
    columns=['legacy_dropback_attempts','excluded_no_play','excluded_two_point','excluded_sack','pass_attempts','remaining_flag_difference','passing_yards','completions','passing_tds']
    return p.groupby(['game_id','posteam','passer_player_id'])[columns].sum().reset_index().rename(columns={'posteam':'team','passer_player_id':'player_id'})


def comparison(frame,actual,incumbent,candidate,*,area,population):
    if frame.empty:return {'area':area,'population':population,'rows':0,'decision':'BLOCKED_NO_POPULATION'}
    old=frame[incumbent]-frame[actual];new=frame[candidate]-frame[actual]
    ci=cluster_ci(frame,new.abs()-old.abs(),seasons='season' in frame and frame.season.nunique()>1)
    return {'area':area,'population':population,'rows':len(frame),'games':frame.game_id.nunique(),
        'incumbent_mae':old.abs().mean(),'challenger_mae':new.abs().mean(),'mae_delta':new.abs().mean()-old.abs().mean(),
        'delta_ci_low':ci[0],'delta_ci_high':ci[1],'incumbent_bias':old.mean(),'challenger_bias':new.mean(),
        'incumbent_overprediction':old.gt(0).mean(),'challenger_overprediction':new.gt(0).mean(),
        'bias_noninferior':abs(new.mean())<=abs(old.mean())+.01,'mae_supported':ci[1]<0 if ci[1] is not None else False}


def starter_comparisons(root,output):
    original=root/'reports/rigorous_benchmark_week7_20261022/final'
    live=pd.read_csv(original/'official_players.csv')
    proof=json.loads((root/'frontend/releases/frontend-v3-1-57e41c99d7a69eb1/data/player_presentation.json').read_text())['players']
    live['starter_evidence']=False
    for i,r in live.iterrows():
        p=proof.get(f'{r.game_id}|{r.player_id}|{r.team}',{})
        live.loc[i,'starter_evidence']=(p.get('expected_starter') is True and p.get('eligible') is True
            and p.get('prediction_source_run_id')==r.source_run_id)
    models=joblib.load(original/'models/position-affine-mean-v1.joblib')
    rows=[];parts=[]
    for area,position,target,field in [('qb_rushing','QB','rushing_yards','expected_rush_yards'),('te_receiving','TE','receiving_yards','expected_receiving_yards')]:
        b=live[live.position.eq(position)].copy()
        b['challenger_mean']=apply_correction(b[field],models[area]['coefficients'])
        for population,part in [('ALL_FROZEN_FORECASTS',b),('FROZEN_EXPECTED_STARTER',b[b.starter_evidence])]:
            rows.append(comparison(part,target,field,'challenger_mean',area=area,population=population))
        b['area']=area;parts.append(b)
    pd.concat(parts).to_csv(output/'live_starter_comparison_rows.csv',index=False)
    pd.DataFrame(rows).to_csv(output/'live_starter_comparisons.csv',index=False)
    # Reuse archived forward-fold predictions, do not refit coefficients.
    raw=pd.read_csv(root/'NFL Fantasy/2025 Stats/depth_charts_2025.csv',low_memory=False)
    historical=[];historical_rows=[]
    for area in ['qb_rushing','te_receiving']:
        d=pd.read_csv(original/(area+'_chronological_predictions.csv'))
        d=d[d.season.eq(2025)].copy();d['cutoff_utc']=(pd.to_datetime(d.kickoff_utc,utc=True)-pd.Timedelta(hours=24)).astype(str)
        d=asof_depth(raw,d)
        target='rushing_yards' if area=='qb_rushing' else 'receiving_yards'
        b=d[d.expected_starter_depth]
        historical_rows.append(comparison(b,'actual_'+target,'pred_'+target,'mean_candidate',area=area,population='2025_TIMESTAMPED_DEPTH_STARTER; AVAILABILITY_UNPROVED'))
        historical.append(d.assign(area=area))
    pd.concat(historical).to_csv(output/'historical_starter_rows.csv',index=False)
    pd.DataFrame(historical_rows).to_csv(output/'historical_starter_comparisons.csv',index=False)
    # Threshold outcomes remain separate from mean/opportunity metrics.
    quotes=pd.read_csv(original/'paired_player_lines.csv')
    merged=quotes.merge(pd.concat(parts)[KEYS+['area','starter_evidence','challenger_mean','rush_distribution_sd','receiving_distribution_sd']],
        left_on=['game_id','player_id','team'],right_on=KEYS,validate='many_to_one')
    merged=merged[((merged.area=='qb_rushing')&(merged.market=='RUSHING_YARDS'))|((merged.area=='te_receiving')&(merged.market=='RECEIVING_YARDS'))]
    sd=np.where(merged.area.eq('qb_rushing'),merged.rush_distribution_sd,merged.receiving_distribution_sd)
    merged['challenger_over_probability']=norm.sf((merged.line-merged.challenger_mean)/sd)
    merged['probability_method']='DIAGNOSTIC_ONLY unchanged incumbent SD; not certified candidate distribution'
    merged.to_csv(output/'threshold_rows.csv',index=False)
    metrics=[]
    for area,b in merged.groupby('area'):
        for label,p in [('ALL_MATCHED_LINES',b),('FROZEN_EXPECTED_STARTER',b[b.starter_evidence])]:
            p=p.dropna(subset=['actual_over','model_over_probability','challenger_over_probability'])
            if p.empty:continue
            old=(p.model_over_probability-p.actual_over)**2;new=(p.challenger_over_probability-p.actual_over)**2
            ci=cluster_ci(p,new-old)
            metrics.append({'area':area,'population':label,'book_lines':len(p),'player_games':len(p.drop_duplicates(KEYS)),
                'incumbent_brier':old.mean(),'challenger_brier':new.mean(),'delta_ci_low':ci[0],'delta_ci_high':ci[1],
                'incumbent_probability_bias':(p.model_over_probability-p.actual_over).mean(),
                'challenger_probability_bias':(p.challenger_over_probability-p.actual_over).mean(),
                'distribution_certified':False})
    pd.DataFrame(metrics).to_csv(output/'threshold_metrics.csv',index=False)


def reconcile_passing(root,output):
    import pyarrow.parquet as pq
    path=root/'NFL Fantasy/2025 Stats/pbp_2025.parquet'
    columns=['game_id','play_id','posteam','passer_player_id','passing_yards','complete_pass','pass_attempt','sack','play_type','two_point_attempt','pass','pass_touchdown','season_type']
    raw=pq.read_table(path,columns=columns).to_pandas();raw=raw[raw.season_type.eq('REG')]
    labels=passing_labels(raw)
    history=pd.read_csv(root/'artifacts/research/phase7/qb_passing_history.csv')
    old=history[history.season.eq(2025)]
    shared=old.merge(labels,on=KEYS,validate='one_to_one',suffixes=('_archived','_contract'))
    for field in ['pass_attempts','passing_yards','passing_tds']:
        if not np.allclose(shared[field+'_archived'],shared[field+'_contract']):raise ValueError('Passing incumbent PBP labels differ from repaired contract')
    games=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    mapping=pd.concat([games[games.season.eq(2025)][['game_id','week','home_team']].rename(columns={'home_team':'team'}),
        games[games.season.eq(2025)][['game_id','week','away_team']].rename(columns={'away_team':'team'})])
    weekly=pd.read_csv(root/'NFL Fantasy/2025 Stats/weekly_2025.csv',low_memory=False)
    weekly=weekly[weekly.pass_attempts.gt(0)].merge(mapping,on=['week','team'],validate='many_to_one')
    audit=weekly[KEYS+['pass_attempts']].rename(columns={'pass_attempts':'legacy_weekly_attempts'}).merge(labels,on=KEYS,validate='one_to_one')
    audit['original_difference']=audit.legacy_weekly_attempts-audit.pass_attempts
    audit['converter_reproduced']=audit.legacy_weekly_attempts.eq(audit.legacy_dropback_attempts)
    audit['explained_difference']=audit.excluded_no_play+audit.excluded_two_point+audit.excluded_sack+audit.remaining_flag_difference
    audit['fully_reconciled']=audit.original_difference.eq(audit.explained_difference)&audit.converter_reproduced
    original_keys=set(map(tuple,old[KEYS].to_numpy()))
    audit['in_original_reconciliation']=list(map(lambda key:key in original_keys,map(tuple,audit[KEYS].to_numpy())))
    audit.to_csv(output/'passing_2025_label_reconciliation.csv',index=False)
    labels.to_csv(output/'passing_2025_contract_labels.csv',index=False)
    result={'status':'PASS' if audit.fully_reconciled.all() else 'BLOCKED_UNEXPLAINED_DIFFERENCE',
        'matched_weekly_rows':len(audit),'original_mismatches':int(audit.original_difference.ne(0).sum()),
        'previously_reported_population':int(audit.in_original_reconciliation.sum()),
        'previously_reported_mismatches':int((audit.in_original_reconciliation&audit.original_difference.ne(0)).sum()),
        'additional_all_excluded_passer_contexts':int((~audit.in_original_reconciliation).sum()),
        'unexplained':int((~audit.fully_reconciled).sum()),'archived_passing_labels_unchanged':True,
        'excluded_no_play':int(audit.excluded_no_play.sum()),'excluded_two_point':int(audit.excluded_two_point.sum()),
        'excluded_sacks':int(audit.excluded_sack.sum()),'remaining_flag_difference':int(audit.remaining_flag_difference.sum()),
        'new_labels_include_yards_and_completions':True,'original_weekly_file_modified':False,'retraining_required':False,
        'source_sha256':sha256_file(path),'converter_sha256':sha256_file(root/'NFL Fantasy/pbp_to_weekly_2025.py')}
    json_write(output/'passing_label_resolution.json',result)
    return raw,labels


def passing_evaluation(root,output):
    # Existing research model already uses the corrected PBP label contract.
    oos=pd.read_csv(root/'artifacts/research/phase7/qb_passing_oos.csv')
    history=pd.read_csv(root/'artifacts/research/phase7/qb_passing_history.csv')
    from nfl2026.phase7.passing import lag_passing
    d=lag_passing(oos,history)
    d=d[d.season.eq(2025)].copy();d['position']='QB'
    d['cutoff_utc']=(pd.to_datetime(d.kickoff_utc,utc=True)-pd.Timedelta(hours=24)).astype(str)
    depth=pd.read_csv(root/'NFL Fantasy/2025 Stats/depth_charts_2025.csv',low_memory=False)
    d=asof_depth(depth,d)
    # No outcome-based starter selection. Missing personal history uses only prior folds.
    train=oos[oos.season.lt(2025)]
    d['lag_baseline']=d.lag_passing_yards.fillna(train.passing_yards.median())
    metrics=[]
    for label,b in [('ALL_NOMINAL_QBS',d),('2025_TIMESTAMPED_QB1',d[d.expected_starter_depth])]:
        metrics.append(comparison(b,'passing_yards','lag_baseline','pred_passing_yards',area='incumbent_passing_research_vs_lag',population=label))
    d.to_csv(output/'passing_existing_model_starter_rows.csv',index=False)
    pd.DataFrame(metrics).to_csv(output/'passing_existing_model_metrics.csv',index=False)
    # Forward residual calibration: 2023 -> 2024, 2023/24 -> 2025. No nominal-QB
    # intervals can be called expected-starter certified without that population.
    intervals=[]
    for season in [2024,2025]:
        past=oos[oos.season.lt(season)];test=oos[oos.season.eq(season)].copy()
        residual=past.passing_yards-past.pred_passing_yards
        low,high=np.quantile(residual,[.1,.9])
        test['p10']=np.maximum(0,test.pred_passing_yards+low);test['p90']=np.maximum(0,test.pred_passing_yards+high)
        test['interval_training_max']=pd.to_datetime(past.kickoff_utc,utc=True).max().isoformat()
        test['interval_population']='NOMINAL_QB; NOT EXPECTED_STARTER_CERTIFIED'
        intervals.append(test)
    result=pd.concat(intervals,ignore_index=True);result.to_csv(output/'passing_forward_intervals.csv',index=False)
    summary=[]
    for season,b in result.groupby('season'):
        summary.append({'season':season,'rows':len(b),'coverage80':((b.passing_yards>=b.p10)&(b.passing_yards<=b.p90)).mean(),
            'width80':(b.p90-b.p10).mean(),'starter_population_certified':False,'market_threshold_calibration':'NO_PASSING_QUOTES'})
    pd.DataFrame(summary).to_csv(output/'passing_interval_metrics.csv',index=False)


def replay_locked_player_means(root,output):
    """Pure inference against exact locked rows, never generate_bundle/registry."""
    from nfl2026.production.core import filter_current_candidates,redistribute_unavailable_opportunity,verify_lock
    games=pd.read_csv(root/'reports/rigorous_benchmark_week7_20261022/final/official_games.csv')
    records=[]
    for (week,rid),selected in games.groupby(['week','source_run_id']):
        run=root/f'predictions/2026/week_{week:02d}'/rid;meta=json.loads((run/'metadata.json').read_text())
        lock=root/f"locks/2026/week_{week:02d}/{meta['lock_id']}";lm=json.loads((lock/'data_lock.json').read_text())
        name='player_pregame_universe_features_run_scoped.csv' if lm.get('run_scoped_replacement') else 'player_pregame_universe_features.csv'
        source=lock/'inputs'/name
        row={'week':int(week),'source_run_id':rid,'selected_games':len(selected),'locked_features':source.exists()}
        if not source.exists():records.append({**row,'status':'BLOCKED_EXACT_INPUTS_NOT_ARCHIVED'});continue
        if verify_lock(lock):raise ValueError('Replay lock changed')
        modelrow=meta['model_ids']['player'];version=modelrow['version']
        modelpath=root/f'artifacts/models/player-{version}.joblib'
        if sha256_file(modelpath)!=modelrow['hash']:raise ValueError('Replay model artifact differs')
        artifact=joblib.load(modelpath);features=pd.read_csv(source,low_memory=False)
        features=features[features.season.eq(2026)&features.week.eq(week)].copy()
        # Partial source locks have an explicit eligible fixture universe.
        if lm.get('eligible_game_ids'):features=features[features.game_id.isin(lm['eligible_game_ids'])]
        read=lambda filename:pd.read_csv(lock/'inputs'/filename,low_memory=False)
        roster=read('weekly_roster_2026.csv');injuries=read('injuries_2026.csv')
        tx=read('transactions_2026.csv') if (lock/'inputs/transactions_2026.csv').exists() else None
        try:
            features=filter_current_candidates(features,roster,injuries,int(week),transactions=tx,cutoff=meta['cutoff_utc'])
        except ValueError as exc:
            records.append({**row,'status':'BLOCKED_FROZEN_SOURCE_CONFLICT','reason':str(exc)})
            continue
        features=features[features.position.isin(['QB','RB','FB','WR','TE'])].reset_index(drop=True)
        with threadpool_limits(2):
            expected={target:np.clip(features['prior_'+target].fillna(0).to_numpy() if isinstance(model,dict)
                else model.predict(features[artifact['features']]),0,None) for target,model in artifact['models'].items()}
        unavailable=set() if lm.get('run_scoped_replacement') else set(injuries.loc[injuries.game_status.astype(str).str.upper().isin(['OUT','IR','PUP','INACTIVE']),'player_id'].dropna().astype(str))
        features,expected=redistribute_unavailable_opportunity(features,expected,unavailable)
        replay=features[KEYS].copy()
        for t in ['carries','targets','rushing_yards','receiving_yards']:replay[t]=expected[t]
        saved=pd.read_csv(run/'players.csv').rename(columns={'expected_carries':'carries','expected_targets':'targets','expected_rush_yards':'rushing_yards','expected_receiving_yards':'receiving_yards'})
        merged=replay.merge(saved[KEYS+['carries','targets','rushing_yards','receiving_yards']],on=KEYS,how='outer',suffixes=('_replay','_saved'),indicator=True,validate='one_to_one')
        identity=merged._merge.eq('both').all();differences={t:float((merged[t+'_replay']-merged[t+'_saved']).abs().max()) for t in ['carries','targets','rushing_yards','receiving_yards']}
        status='PASS' if identity and max(differences.values())<1e-8 else 'BLOCKED_REPLAY_DIFFERS'
        records.append({**row,'status':status,'all_run_rows':len(saved),'identity_equal':bool(identity),'max_absolute_difference':max(differences.values()),'source_sha256':sha256_file(source),'model_sha256':sha256_file(modelpath)})
    pd.DataFrame(records).to_csv(output/'exact_live_replay.csv',index=False)
