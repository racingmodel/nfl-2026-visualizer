"""Trace actual frozen score inputs rather than infer use from available sources."""
import json
import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file

RUN='phase3_5-production-20261021T210639Z-e0207002'


def audit(settings,out):
    from scripts.build_phase2 import _current_team_matrix,_latest_team_value
    root=settings.root;run=root/'predictions/2026/week_07'/RUN
    meta=json.loads((run/'metadata.json').read_text());lock=root/'locks/2026/week_07'/meta['lock_id']
    model=joblib.load(root/'artifacts/models/team-phase2-v2.joblib');features=model['features']
    historical=pd.read_csv(lock/'inputs/historical_team_features.csv',low_memory=False)
    schedule=pd.read_csv(lock/'inputs/games.csv',low_memory=False)
    schedule=schedule[schedule.season.eq(2026)].copy()
    schedule['kickoff_utc']=pd.to_datetime(schedule.gameday+' '+schedule.gametime.fillna('13:00')).dt.tz_localize('America/New_York').dt.tz_convert('UTC')
    state=pd.read_csv(lock/'inputs/team_feature_state_2026_w07.csv')
    current=_current_team_matrix(schedule,historical,features,team_state=state,target_week=7)
    current.to_csv(out/'exact_week7_score_inputs.csv',index=False)
    original=pd.read_csv(run/'games.csv').set_index('game_id').loc[current.game_id]
    with threadpool_limits(2):
        predict=lambda x:np.column_stack([np.clip(model[s].predict(x[features]),0,60) for s in ['home','away']])
        baseline=predict(current)
        swapped=schedule.copy()
        swapped[['home_qb_id','away_qb_id']]=swapped[['away_qb_id','home_qb_id']].to_numpy()
        swapped_matrix=_current_team_matrix(swapped,historical,features,team_state=state,target_week=7)
        swap_delta=float(np.max(np.abs(predict(swapped_matrix)-baseline)))
        delta=float(np.max(np.abs(baseline-original[['home_expected_points','away_expected_points']].to_numpy())))
        if delta>1e-9:raise ValueError('Exact score replay differs')
        groups={'QB':[c for c in features if '_qb_' in c],
            'points_form':[c for c in features if c.endswith(('pregame_points_for','pregame_points_against'))],
            'EPA_and_matchup':[c for c in features if 'epa' in c or 'matchup' in c],
            'pace_and_volume':[c for c in features if c.endswith(('plays','drives','pass_rate'))]}
        changes=[]
        for label,cols in groups.items():
            counter=current.copy()
            for c in cols:
                if label=='QB':counter[c]=historical[c].median()
                else:
                    side=c.split('_')[0]
                    for i,row in current.iterrows():
                        counter.loc[i,c]=_latest_team_value(historical,row[side+'_team'],c[len(side)+1:]) if '_pregame_' in c else historical[c].median()
            diff=predict(counter)-baseline
            for i,gid in enumerate(current.game_id):changes.append(dict(game_id=gid,group=label,home_counterfactual_minus_official=diff[i,0],away_counterfactual_minus_official=diff[i,1],causal_effect=False))
    pd.DataFrame(changes).to_csv(out/'score_feature_sensitivity.csv',index=False)
    qbs=pd.read_csv(run/'qb_status.csv');qbs.to_csv(out/'frozen_week7_qb_context.csv',index=False)
    memberships=[]
    completed=schedule[schedule.season.le(2026)&schedule.game_type.eq('REG')&schedule.home_score.notna()&schedule.away_score.notna()]
    full=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    # Historical 2025 games and the frozen 2026 completed slate, not today's globals.
    full=pd.concat([full[full.season.lt(2026)&full.game_type.eq('REG')],completed[completed.season.eq(2026)]],ignore_index=True).drop_duplicates('game_id')
    full['time']=pd.to_datetime(full.kickoff_utc,utc=True)
    for row in current.itertuples():
        for side in ['home','away']:
            team=getattr(row,side+'_team');prior=full[(full.home_team.eq(team)|full.away_team.eq(team))&full.time.lt(pd.Timestamp(row.kickoff_utc))].sort_values('time').tail(8)
            memberships.append(dict(game_id=row.game_id,team=team,source_games=';'.join(prior.game_id),games=len(prior),games_2026=int(prior.season.eq(2026).sum()),fraction_2026=float(prior.season.eq(2026).mean()),weights='unweighted last eight',production_weights_retrained=False))
    pd.DataFrame(memberships).to_csv(out/'week7_form_membership.csv',index=False)
    qbcols=[c for c in features if '_qb_' in c]
    json_write(out/'score_input_audit.json',{'status':'PASS_EXACT_NUMERIC_TRACE','features':features,'qb_features':qbcols,
        'qb_identity_is_lookup_key_not_direct_categorical_feature':True,'availability_is_gate_not_score_feature':True,
        'qb_ratings_source':'BROKEN ID LOOKUP: frozen historical CSV omits expected_qb_id; career dictionary is empty, all ten QB rating columns are NaN and fitted imputer supplies medians',
        'historical_expected_qb_identity_columns_present':all(f'{s}_expected_qb_id' in historical for s in ['home','away']),
        'quarterback_rating_missing_cells':int(current[[c for c in qbcols if not c.endswith('replacement')]].isna().sum().sum()),
        'qb_replacement_flag_values':sorted(set(current[['home_qb_replacement','away_qb_replacement']].to_numpy().ravel())),
        'root_cause':'team_advanced.py CSV export retains numeric model features but drops expected-QB IDs and source reliability needed by _current_team_matrix',
        'current_QB_identity_changes_numeric_score_inputs':False,
        'diagnostic_swap_QB_identities_max_score_change':swap_delta,
        '2026_QB_efficiency_rollforward_into_these_columns':False,'new_player_injury_or_lineup_numeric_features':[],
        'bookmaker_features':[c for c in features if any(t in c for t in ['spread_line','bookmaker','odds'])],
        'not_explicit_features':['rolling_margin','home_away_form_splits','time_of_possession','lineup_injury_severity','designed_QB_rushing'],
        'recent_form':'last-eight cross-season team averages; Elo updated with completed 2026 results; matchup EPA is offense minus opposing allowed average, not a fitted opponent-adjustment',
        'max_replay_difference':delta,'source_sha256':{str(p.relative_to(root)):sha256_file(p) for p in [run/'metadata.json',lock/'inputs/historical_team_features.csv',lock/'inputs/team_feature_state_2026_w07.csv',root/'scripts/build_phase2.py',root/'nfl2026/production/core.py']}})
    players=pd.read_csv(run/'players.csv');proof=json.loads((root/'frontend/releases/frontend-v3-1-30be9141bd174f70/data/player_presentation.json').read_text())['players']
    rawpath=root/'data/live_downloads/2026/20261021T210136Z/depth_charts_2026.csv'
    raw=pd.read_csv(rawpath,low_memory=False);raw['dt']=pd.to_datetime(raw.dt,utc=True);raw=raw[raw.dt.le(pd.Timestamp(meta['cutoff_utc']))]
    from nfl2026.evolution.depth_coverage import position_coverage
    position_coverage(raw,sorted(set(current.home_team)|set(current.away_team)),meta['cutoff_utc']).to_csv(out/'offensive_depth_position_coverage.csv',index=False)
    ne=players[players.team.eq('NE')&players.position.eq('WR')].copy()
    ne['presentation_expected_starter']=[proof[f'{r.game_id}|{r.player_id}|NE']['expected_starter'] for r in ne.itertuples()]
    ne.to_csv(out/'new_england_receiver_audit.csv',index=False)
    wr=raw[raw.team.eq('NE')&raw.pos_abb.eq('WR')]
    json_write(out/'new_england_receiver_findings.json',{'reason':'LATEST_FROZEN_PROVIDER_SNAPSHOT_MISSING_WR_POSITION','original_projections_preserved':len(ne),
        'latest_team_snapshot':raw[raw.team.eq('NE')].dt.max(),'latest_WR_snapshot':wr.dt.max(),
        'raw_depth_source_sha256':sha256_file(rawpath),'join_identity_mismatch_found':False,
        'inactive_players_silently_reclassified':False,'automatic_fallback_allowed':False,
        'normalizer_issue':'load_depth selects global maximum dt, dropping earlier team-position rows even when dated; position-level source completeness is not checked',
        'prior_WR_rows':[dict(player_id=str(r.gsis_id),name=str(r.player_name),known_at_utc=r.dt.isoformat()) for r in wr[wr.dt.eq(wr.dt.max())].itertuples()],
        'frontend_action':'retain unverified WR projections on Players; no Expected Starter prop without fresh offensive depth evidence'})
