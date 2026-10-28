"""Frozen-cutoff eligibility audit and non-numerical public presentation proof."""
import json
from pathlib import Path
import pandas as pd
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file

INACTIVE={'OUT','IR','RESERVE','PUP','INACTIVE','RELEASED','WAIVED','PRACTICE_SQUAD'}
POSITIONS={'QB','RB','FB','WR','TE'}


def role_evidence(player, roster, depth, injuries, teams):
    """Do not infer starters from yards, names or only one-per-position rank."""
    pid,team=str(player['player_id']),str(player['team'])
    r=roster[roster.gsis_id.astype(str).eq(pid)]
    depth_position=player['position']
    if len(r)==1 and depth_position=='RB' and r.iloc[0].get('depth_chart_position')=='FB':depth_position='FB'
    d=depth[depth.player_id.astype(str).eq(pid)&depth.team.astype(str).eq(team)&depth.position.eq(depth_position)]
    i=injuries[injuries.player_id.astype(str).eq(pid)&injuries.team.astype(str).eq(team)] if 'player_id' in injuries else pd.DataFrame()
    problems=[]
    if len(r)!=1:problems.append('ROSTER_IDENTITY_MISSING_OR_AMBIGUOUS')
    elif str(r.iloc[0].team)!=team:problems.append('WRONG_TEAM')
    if team not in teams or player['position'] not in POSITIONS:problems.append('UNSUPPORTED_TEAM_OR_POSITION')
    status=str(r.iloc[0].status).upper() if len(r)==1 else 'UNKNOWN'
    tx=str(r.iloc[0].get('current_transaction_state','')).upper() if len(r)==1 else ''
    unavailable=status in INACTIVE or tx in INACTIVE
    report_status=set(i.get('game_status',pd.Series(dtype=str)).fillna('UNKNOWN').astype(str).str.upper())
    unavailable=unavailable or bool(report_status&INACTIVE) or str(player.get('injury_status','')).upper() in INACTIVE or str(player.get('availability_status','')).upper() in INACTIVE
    if unavailable:problems.append('DEFINITIVELY_INACTIVE')
    if status not in {'ACT','ACTIVE'} and not unavailable:problems.append('ROSTER_STATUS_UNKNOWN')
    verified_depth=len(d)>0 and d.depth_source.eq('NFLVERSE_CURRENT').all()
    depth_starter=verified_depth and d.depth_role.eq('STARTER').any() and pd.to_numeric(d.depth_order,errors='coerce').eq(1).any()
    declared=player.get('role_status')=='EXPECTED_STARTER' and player.get('depth_role')=='STARTER'
    role_error=bool(verified_depth and (depth_starter!=declared))
    eligible=not problems
    return {'eligible':eligible,'expected_starter':bool(eligible and depth_starter),
        'presentation_role_correction':bool(role_error and depth_starter),
        'role_mismatch':role_error,'definitively_ineligible':unavailable,'issues':'; '.join(problems),
        'roster_status':status,'transaction_state':tx if tx!='NAN' else 'UNKNOWN',
        'depth_verified':bool(verified_depth),'depth_starter':bool(depth_starter),
        'matched_depth_position':depth_position,
        'depth_slots':';'.join(d.get('depth_slot',pd.Series(dtype=str)).fillna('').astype(str)),
        'injury_source_state':player.get('injury_source_state','SOURCE_MISSING'),
        'classification':'HISTORICAL_FROZEN_ELIGIBILITY_ISSUE' if problems else 'HISTORICAL_FROZEN_ROLE_ISSUE' if role_error else 'ROLE_EVIDENCE_UNAVAILABLE' if declared and not verified_depth else 'PRESENTATION_ONLY_ROTATIONAL_PLAYER' if not declared else 'EXPECTED_STARTER_VERIFIED'}


def audit_frozen_players(settings, run_id, week, season=2026):
    from nfl2026.production.core import verify_lock
    run=settings.prediction_root/f'{season}/week_{week:02d}'/run_id
    meta=json.loads((run/'metadata.json').read_text())
    for name,digest in meta['file_hashes'].items():
        if sha256_file(run/name)!=digest:raise ValueError('Frozen forecast modified')
    if (meta['run_id'], int(meta['season']), int(meta['week'])) != (run_id, season, week):
        raise ValueError('Frozen forecast identity mismatch')
    lock=settings.root/f"locks/{season}/week_{week:02d}"/meta['lock_id']
    if verify_lock(lock):raise ValueError('Frozen evidence lock invalid')
    cutoff=pd.Timestamp(meta['cutoff_utc'])
    sources=pd.read_csv(lock/'lock_manifest.csv')
    derived=[]
    lock_meta=json.loads((lock/'data_lock.json').read_text())
    if lock_meta.get('fixture_gate_version'):
        from nfl2026.production.partial_gate import validate_partial_certificate, GATE_VERSION
        if lock_meta['fixture_gate_version']!=GATE_VERSION or validate_partial_certificate(run,meta):
            raise ValueError('Frozen partial fixture certificate invalid')
        # These are generated certificate artifacts, not provider observations.
        # Their actual certification timestamps may follow the feature cutoff;
        # never invent provider timestamps for them or exempt other source rows.
        for source,filename,hash_key in [('game_eligibility','game_eligibility.csv','game_eligibility_sha256'),
                                        ('fixture_gate_audit','fixture_gate_audit.json','fixture_gate_audit_sha256')]:
            rows=sources[sources.source.eq(source)]
            expected=lock/'inputs'/filename
            if (len(rows)!=1 or (settings.root/str(rows.iloc[0].locked_path)).resolve()!=expected.resolve()
                or rows.iloc[0].sha256!=lock_meta.get(hash_key) or sha256_file(expected)!=lock_meta.get(hash_key)
                or str(rows.iloc[0].data_status)!='READY'):
                raise ValueError('Frozen derived certificate binding invalid: '+source)
            derived.append({'source':source,'sha256':lock_meta[hash_key],'timestamp_policy':'certified derived artifact; not provider data'})
        sources=sources[~sources.source.isin([row['source'] for row in derived])]
    for name in ('known_at_utc','retrieved_at_utc'):
        times=pd.to_datetime(sources[name],utc=True,errors='coerce',format='mixed')
        if times.isna().any() or times.gt(cutoff).any():raise ValueError('Future/unknown frozen source timestamps')
    inputs=lock/'inputs'
    roster=pd.read_csv(inputs/f'weekly_roster_{season}.csv',low_memory=False);roster=roster[roster.week.eq(week)]
    depth=pd.read_csv(inputs/f'depth_charts_{season}.csv',low_memory=False)
    injuries=pd.read_csv(inputs/f'injuries_{season}.csv',low_memory=False)
    injuries=injuries[injuries.get('record_type',pd.Series('',index=injuries.index)).eq('PLAYER_REPORT')]
    transactions=pd.read_csv(inputs/f'transactions_{season}.csv',low_memory=False)
    for field in ('known_at_utc','retrieved_at_utc','event_at_utc'):
        t=pd.to_datetime(transactions[field],utc=True,errors='coerce',format='mixed')
        if t.isna().any() or t.gt(cutoff).any():raise ValueError('Future/unknown transaction evidence')
    players=pd.read_csv(run/'players.csv');games=pd.read_csv(run/'games.csv')
    if players.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate forecast keys')
    rows=[];proof={}
    for p in players.to_dict('records'):
        game=games[games.game_id.eq(p['game_id'])]
        if len(game)!=1:raise ValueError('Unknown player game identity')
        teams={game.iloc[0].home_team,game.iloc[0].away_team}
        evidence=role_evidence(p,roster,depth,injuries,teams)
        from nfl2026.live.providers.transactions import _key
        tx=transactions[transactions.player_id.astype(str).eq(str(p['player_id'])) | (transactions.player_id.isna() & transactions.player_name.map(_key).eq(_key(p['player_name'])) & (transactions.from_team.eq(p['team'])|transactions.to_team.eq(p['team'])))]
        if len(tx):
            tx=tx.assign(_time=pd.to_datetime(tx.event_at_utc,utc=True,format='mixed'),_priority=tx.get('transaction_state',pd.Series('',index=tx.index)).astype(str).str.upper().isin(INACTIVE).astype(int)).sort_values(['_time','_priority'],kind='stable')
            event=tx.iloc[-1];state=str(event.get('transaction_state','UNKNOWN')).upper()
            evidence.update(latest_transaction_state=state,latest_transaction_date=str(event.event_at_utc),transaction_source=event.source)
            if state in INACTIVE:
                evidence.update(eligible=False,expected_starter=False,definitively_ineligible=True,
                    issues=(evidence['issues']+'; LATEST_LOCKED_TRANSACTION_INACTIVE').strip('; '),classification='HISTORICAL_FROZEN_ELIGIBILITY_ISSUE')
        else:evidence['latest_transaction_state']='NO_MATCHED_EVENT; COVERAGE_INCOMPLETE'
        evidence.update(prediction_source_run_id=run_id,cutoff_utc=meta['cutoff_utc'],lock_id=meta['lock_id'])
        key=f"{p['game_id']}|{p['player_id']}|{p['team']}";proof[key]=evidence
        rows.append({**{k:p[k] for k in ['game_id','player_id','player_name','team','position','depth_role','role_status','role_confidence','injury_status','reconciled_td_probability','expected_carries','expected_targets','expected_rush_yards','expected_receiving_yards']},**evidence})
    audit=pd.DataFrame(rows)
    summary={'players_checked':len(audit),'incorrect_role_assignments':int(audit.role_mismatch.sum()),
        'definitively_ineligible_players':int(audit.definitively_ineligible.sum()),'expected_starters':int(audit.expected_starter.sum()),
        'presentation_only_rotational_players':int(audit.classification.eq('PRESENTATION_ONLY_ROTATIONAL_PLAYER').sum()),
        'starter_role_evidence_unavailable':int(audit.classification.eq('ROLE_EVIDENCE_UNAVAILABLE').sum()),
        'identity_or_eligibility_issues':int((~audit.eligible).sum()),'unknown_injury_rows':int(audit.injury_status.eq('UNKNOWN').sum()),
        'source_manifest_sha256':sha256_file(lock/'lock_manifest.csv'),'players_sha256':sha256_file(run/'players.csv'),
        'transaction_coverage':'original frozen warning retained; absence is not proof of no transaction',
        'future_sources_used':False,'frozen_predictions_changed':False}
    summary['derived_certificate_sources']=derived
    return audit,proof,summary


def write_current_audit(settings):
    # The weekly header is an anchor, not the source of every fixture. Starter
    # evidence must come from each selected run's own immutable input lock.
    current=json.loads((settings.root/'frontend/dist/data/current_week.json').read_text())
    from nfl2026.phase7.preview import verify_authoritative_bundle
    verify_authoritative_bundle(settings,current)
    grouped={}
    for game in current['games']:
        run=game.get('prediction_source_run_id') or game.get('prediction_run_id') or current['metadata']['run_id']
        grouped.setdefault(run,set()).add(str(game['game_id']))
    frames=[];proof={};sources={}
    for run,game_ids in grouped.items():
        frame,source_proof,source_summary=audit_frozen_players(settings,run,int(current['metadata']['week']))
        selected=frame[frame.game_id.astype(str).isin(game_ids)].copy()
        keys={f"{row.game_id}|{row.player_id}|{row.team}" for row in selected.itertuples()}
        frames.append(selected)
        proof.update({key:source_proof[key] for key in keys})
        sources[run]=source_summary
    audit=pd.concat(frames,ignore_index=True)
    expected={f"{g['game_id']}|{p['player_id']}|{p['team']}" for g in current['games'] for p in g.get('players',[])}
    # The frozen preview intentionally omits some low-opportunity CSV rows.
    # Audit all selected CSV players, and require coverage of every displayed row.
    if not expected.issubset(proof) or len(proof)!=len(audit) or audit.duplicated(['game_id','player_id','team']).any():
        raise ValueError('Current starter audit does not exactly cover selected immutable players')
    summary={'prediction_anchor_run_id':current['metadata']['run_id'],'players_checked':len(audit),'displayed_players_checked':len(expected),
        'incorrect_role_assignments':int(audit.role_mismatch.sum()),
        'definitively_ineligible_players':int(audit.definitively_ineligible.sum()),'expected_starters':int(audit.expected_starter.sum()),
        'presentation_only_rotational_players':int(audit.classification.eq('PRESENTATION_ONLY_ROTATIONAL_PLAYER').sum()),
        'starter_role_evidence_unavailable':int(audit.classification.eq('ROLE_EVIDENCE_UNAVAILABLE').sum()),
        'identity_or_eligibility_issues':int((~audit.eligible).sum()),'unknown_injury_rows':int(audit.injury_status.eq('UNKNOWN').sum()),
        'source_audits':sources,'future_sources_used':False,'frozen_predictions_changed':False}
    audit.to_csv(settings.report_root/'phase75_player_eligibility_audit.csv',index=False)
    json_write(settings.report_root/'phase75_player_integrity.json',summary)
    return proof,summary
