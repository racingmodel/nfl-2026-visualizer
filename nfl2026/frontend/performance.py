"""Presentation of already evaluated official forecasts; no model/ledger writes.

The postgame archive's per-fixture selection supersedes an older weekly freeze
when a partial refresh occurred after that freeze. Original metric cohorts stay
explicit: all-player yardage averages cannot enter opportunity-filtered totals.
"""
import json

import numpy as np
import pandas as pd

from nfl2026.utilities.io import sha256_file


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def team_summary(frame):
    margin = frame.home_score-frame.away_score
    non_tie = margin.ne(0)
    probability = frame.home_win_probability[non_tie]
    outcome = margin[non_tie].gt(0).astype(int)
    return {'rows':len(frame), 'team_score_mae':float(pd.concat([
        (frame.home_expected_points-frame.home_score).abs(),
        (frame.away_expected_points-frame.away_score).abs()]).mean()),
        'margin_mae':float((frame.model_margin-margin).abs().mean()),
        'total_mae':float((frame.model_total-frame.home_score-frame.away_score).abs().mean()),
        'winner_accuracy':float((probability.gt(.5)==outcome).mean()) if len(outcome) else None,
        'win_brier':float(((probability-outcome)**2).mean()) if len(outcome) else None,
        'winner_games':len(outcome), 'winner_correct':int((probability.gt(.5)==outcome).sum())}


def report_row(settings, filename, season, week):
    path = settings.report_root/filename
    frame = pd.read_csv(path)
    rows = frame[frame.season.eq(season)&frame.week.eq(week)]
    if len(rows) > 1:
        raise ValueError('Ambiguous official evaluation report: '+filename)
    return (rows.iloc[0].to_dict() if len(rows) else None), path


def build_performance_presentation(settings):
    from nfl2026.postgame import _completion_mask, inspect_completed_week_actuals
    from nfl2026.production.core import verify_lock
    from nfl2026.frontend.lineage import certified_partial_lineage

    current = read(settings.root/'frontend/dist/data/current_week.json')
    season = int(current['metadata']['season'])
    cached_path = settings.report_root/'phase7_performance.json'
    cached = read(cached_path)
    schedule = pd.read_csv(settings.root/'data/phase2_external/games.csv',low_memory=False)
    metrics, cohorts, source_hashes, game_parts = [], [], {}, []
    def record(path):
        source_hashes[str(path.relative_to(settings.root))] = sha256_file(path)
    record(cached_path)
    for week in sorted(map(int,current['metadata']['available_weeks'])):
        expected = schedule[schedule.season.eq(season)&schedule.week.eq(week)&schedule.game_type.eq('REG')]
        if expected.empty or expected.game_id.duplicated().any():
            raise ValueError('Evaluation fixture population missing/duplicated')
        old = next((m for m in cached['metrics'] if m['segment']==f'WEEK_{week}'),None)
        if not _completion_mask(expected).all():
            if old is not None:
                raise ValueError('Evaluated historical week cannot revert to pending')
            cohorts.append({'week':week,'status':'PENDING_FINALITY','evaluated_games':0,
                'frozen_games':len(expected),'pending_games':len(expected),'actuals_source_state':'WAITING_WEEK_FINALITY'})
            continue
        ingestion = inspect_completed_week_actuals(settings,season,week,expected)
        if ingestion['status'] != 'ALREADY_CURRENT':
            raise ValueError(f'Completed Week {week} lacks validated archived actuals')
        record(ingestion['manifest_path'])
        # This is an immutable evaluation selection saved during actual ingestion,
        # not a new postgame choice of forecasts or an accuracy-based selection.
        choices=[]
        for folder in (settings.artifact_root/f'actuals/{season}/week_{week:02d}').glob('actual-*'):
            if not (folder/'evaluation_run_selection.csv').is_file():
                continue
            am=read(folder/'actuals_manifest.json')
            for name,key in [('team_actuals.csv','team_actuals_sha256'),('player_actuals.csv','player_actuals_sha256')]:
                if sha256_file(folder/name)!=am[key]:raise ValueError('Actual archive hash mismatch')
            actual_games=pd.read_csv(folder/'team_actuals.csv')
            if set(actual_games.game_id)==set(expected.game_id):
                for row in actual_games.itertuples():
                    actual=expected[expected.game_id.eq(row.game_id)].iloc[0]
                    if (actual.home_score,actual.away_score)!=(row.home_score,row.away_score):
                        raise ValueError('Archived final scores differ from validated schedule')
                choices.append(folder)
        if len(choices)>1:raise ValueError('Ambiguous archived official evaluation selection')
        if choices:
            folder=choices[0];selection_path=folder/'evaluation_run_selection.csv'
            selection=pd.read_csv(selection_path).rename(columns={'selected_run_id':'official_prediction_run_id','run_cutoff_utc':'official_prediction_timestamp'})
            record(folder/'actuals_manifest.json');record(folder/'team_actuals.csv');record(folder/'player_actuals.csv')
        else:
            freezes=sorted((settings.artifact_root/f'official_freezes/{season}/week_{week:02d}').glob('freeze-*'))
            if not freezes:raise ValueError('Original evaluated freeze missing')
            selection_path=freezes[-1]/'official_predictions.csv';selection=pd.read_csv(selection_path)
        record(selection_path)
        if selection.game_id.duplicated().any() or set(selection.game_id)!=set(expected.game_id):
            raise ValueError('Official evaluation selection population differs')
        if not pd.to_datetime(selection.official_prediction_timestamp,utc=True).lt(pd.to_datetime(selection.kickoff_utc,utc=True)).all():
            raise ValueError('Evaluation forecast was not pregame')
        # Also bind to the public per-fixture selection, including preserved games.
        bundle_path=settings.root/'frontend/dist/data'/f'week_{week:02d}.json'
        bundle=read(bundle_path) if bundle_path.exists() else current
        certified_partial_lineage(settings,bundle['metadata'])
        displayed={g['game_id']:g.get('prediction_source_run_id') or g.get('prediction_run_id') or bundle['metadata']['run_id'] for g in bundle['games']}
        if dict(zip(selection.game_id,selection.official_prediction_run_id)) != displayed:
            raise ValueError('Archived evaluation differs from original fixture forecast selection')
        parts=[];players=0
        for run_id, group in selection.groupby('official_prediction_run_id'):
            run=settings.prediction_root/f'{season}/week_{week:02d}'/run_id;meta=read(run/'metadata.json');record(run/'metadata.json')
            if meta.get('run_status')!='OFFICIAL_PRODUCTION' or not meta.get('deployment_eligible'):
                raise ValueError('Research/unofficial forecast in performance')
            for name,digest in meta['file_hashes'].items():
                if sha256_file(run/name)!=digest:raise ValueError('Official forecast hash mismatch')
            lock=settings.root/f'locks/{season}/week_{week:02d}'/meta['lock_id']
            if verify_lock(lock):raise ValueError('Evaluation lock hash mismatch')
            if not pd.to_datetime(group.official_prediction_timestamp,utc=True).eq(pd.Timestamp(meta['cutoff_utc'])).all():
                raise ValueError('Archived evaluation cutoff differs from original run')
            ids=set(group.game_id);g=pd.read_csv(run/'games.csv');g=g[g.game_id.isin(ids)].copy()
            if set(g.game_id)!=ids:raise ValueError('Evaluated fixture absent from source run')
            p=pd.read_csv(run/'players.csv');players+=int((p.game_id.isin(ids)&p.position.isin(['QB','RB','FB','WR','TE'])).sum())
            g['source_run_id']=run_id;parts.append(g)
            cohorts.append({'week':week,'run_id':run_id,'status':'EVALUATED','evaluated_games':len(g),
                'frozen_games':len(g),'pending_games':0,'actuals_source_state':'ALREADY_CURRENT',
                'selection_path':str(selection_path.relative_to(settings.root))})
        joined=pd.concat(parts,ignore_index=True).merge(expected[['game_id','home_score','away_score']],on='game_id',validate='one_to_one')
        joined['week']=week;game_parts.append(joined)
        row={'segment':f'WEEK_{week}',**team_summary(joined),'team_status':'EVALUATED',
             'offensive_player_rows':players,'td_rows':0,'td_brier':None,'td_status':'PENDING_EVALUATION','rush_rows':0,'rush_mae':None,
             'receiving_rows':0,'receiving_mae':None,'player_status':'PENDING_EVALUATION'}
        if old is not None:
            for field in ['team_score_mae','margin_mae','total_mae','winner_accuracy','win_brier']:
                if not np.isclose(row[field],old[field],rtol=0,atol=1e-10):
                    raise ValueError('Cached evaluated metric/source mismatch: '+field)
            if players!=old['offensive_player_rows']:raise ValueError('Cached offensive cohort differs')
            for field in ['td_brier','rush_rows','rush_mae','receiving_rows','receiving_mae']:row[field]=old[field]
            row.update(td_rows=players,td_status='EVALUATED',player_status='EVALUATED_OPPORTUNITY_COHORT')
        else:
            team,tp=report_row(settings,f'live_{season}_team_performance.csv',season,week)
            player,pp=report_row(settings,f'live_{season}_player_performance.csv',season,week)
            td,tdp=report_row(settings,f'live_{season}_td_calibration.csv',season,week)
            if team is None:raise ValueError('Completed week lacks official team evaluation output')
            for target,source in [('team_score_mae','score_mae'),('margin_mae','margin_mae'),('total_mae','total_mae'),('winner_accuracy','winner_accuracy'),('win_brier','win_probability_brier')]:
                if int(team['games'])!=len(joined) or not np.isclose(row[target],team[source],rtol=0,atol=1e-10):
                    raise ValueError('Official evaluation output differs from frozen forecasts')
                row[target]=float(team[source])
            record(tp)
            if td is not None:
                if int(td['players'])!=players:raise ValueError('TD evaluation population mismatch')
                row.update(td_brier=float(td['td_brier']),td_rows=players,td_status='EVALUATED');record(tdp)
            if player is not None:
                if int(player['players'])!=players:raise ValueError('Player evaluation population mismatch')
                row['reported_player_evaluation']={k:player[k] for k in ['players','carry_mae','target_mae','rush_yards_mae','receiving_yards_mae']}
                row['player_status']='EVALUATED_ALL_OFFENSIVE; OPPORTUNITY_SUBGROUP_PENDING';record(pp)
        metrics.append(row)
    combined=pd.concat(game_parts,ignore_index=True)
    all_row={'segment':'ALL',**team_summary(combined),'offensive_player_rows':sum(m['offensive_player_rows'] for m in metrics)}
    for kind,value,count in [('td','td_brier','td_rows'),('rush','rush_mae','rush_rows'),('receiving','receiving_mae','receiving_rows')]:
        valid=[m for m in metrics if m.get(value) is not None and m.get(count,0)>0]
        n=sum(m[count] for m in valid);all_row[count]=n
        all_row[value]=sum(m[value]*m[count] for m in valid)/n if n else None
        all_row[kind+'_weeks']=[int(m['segment'].split('_')[1]) for m in valid]
    ledger_path=settings.root/'value_runs/ledger/value_ledger.csv';ledger=pd.read_csv(ledger_path);record(ledger_path)
    blocked=ledger[ledger.settlement_status.eq('SETTLEMENT_BLOCKED')]
    columns=[c for c in ['ledger_entry_id','game_id','player_id','player_name','selection','market_type','settlement_status','settlement_result'] if c in blocked]
    return {'schema':'official-performance-presentation-v1','season':season,'metrics':[all_row,*metrics],
        'cohorts':cohorts,'games':len(combined),'offensive_player_rows':all_row['offensive_player_rows'],
        'source_sha256':source_hashes,'source_policy':'immutable archived evaluation selection; original freeze fallback for previously evaluated historical cohorts',
        'settlements':{'status_counts':ledger.settlement_status.value_counts().to_dict(),
            'blocked_count':len(blocked),'blocked':blocked[columns].fillna('').to_dict('records'),
            'policy':'Blocked selections excluded from settled ROI; no bookmaker nonparticipant outcome invented'},
        'qualifications':['Original pregame forecasts and validated actuals; no retrospective prediction regeneration',
            'Official team and TD Week 6 metrics use the archived 12 refreshed + 2 preserved fixture selection',
            'Team totals pool per-game errors; winner accuracy excludes tied games',
            'TD totals weight each evaluated offensive forecast; research-only V4 results are excluded',
            'Yardage cards retain the prior meaningful-opportunity cohort. Week 6 official all-player yardage metrics are shown separately; comparable subgroups remain pending',
            'No missing player outcomes filled by this presentation builder; original evaluated metric policies remain attached to their source reports',
            'Blocked historical market settlements remain excluded from settled returns and visible below']}
