"""Point-in-time eligibility over immutable forecasts; no opportunity redistribution."""
from datetime import datetime,time,timezone
from zoneinfo import ZoneInfo
import json
import pandas as pd

UNAVAILABLE={'OUT','INACTIVE','INA','IR','PUP','SUSPENDED','RELEASED','WAIVED','CUT','PRACTICE_SQUAD','RES'}
UNCERTAIN={'QUESTIONABLE','DOUBTFUL'}
PROJECTIONS=['expected_carries','expected_targets','expected_rush_yards','expected_receiving_yards','reconciled_td_probability']


def timestamp(value):
    point=pd.Timestamp(value)
    if pd.isna(point) or point.tzinfo is None:raise ValueError('Availability requires an explicit, nonmissing timezone')
    return point.tz_convert('UTC')


def final_report_due(kickoff):
    local=timestamp(kickoff).tz_convert('America/New_York')
    # Thursday final Wednesday; Sunday final Friday; Monday final Saturday.
    before=1 if local.weekday()==3 else 2
    day=(local-pd.Timedelta(days=before)).date()
    return pd.Timestamp(datetime.combine(day,time(16),ZoneInfo('America/New_York'))).tz_convert('UTC')


def validate_events(events,fixtures,asof):
    now=timestamp(asof);lookup=fixtures.set_index('game_id')
    if fixtures.game_id.duplicated().any():raise ValueError('Duplicate fixtures')
    required={'game_id','team','player_id','status','observed_at_utc','known_at_utc','source_url','source_sha256'}
    if events.empty:return events.copy()
    if not required.issubset(events):raise ValueError('Missing availability provenance')
    for r in events.to_dict('records'):
        if r['game_id'] not in lookup.index:raise ValueError('Availability fixture mismatch')
        f=lookup.loc[r['game_id']]
        if r['team'] not in [f.home_team,f.away_team]:raise ValueError('Availability team mismatch')
        observed=timestamp(r['observed_at_utc']);known=timestamp(r['known_at_utc'])
        if known<observed:raise ValueError('Known-at may not predate observation')
        if known>now:raise ValueError('Future availability observation')
        for name in ['published_at_utc','modified_at_utc']:
            if pd.notna(r.get(name)) and r.get(name):
                if timestamp(r[name])>observed:raise ValueError('Future publication/modification')
        if pd.notna(r.get('published_at_utc')) and pd.notna(r.get('modified_at_utc')):
            if timestamp(r['published_at_utc'])>timestamp(r['modified_at_utc']):raise ValueError('Publication order')
        if known>=timestamp(f.kickoff_utc):raise ValueError('Post-kickoff availability evidence')
        if pd.isna(r['player_id']) or not r['player_id'] or len(str(r['source_sha256']))!=64:raise ValueError('Availability identity/hash missing')
    return events.copy()


def resolution(rows,roster_status):
    """Absence/UNKNOWN never clears a positive adverse designation."""
    if roster_status in UNAVAILABLE:return 'UNAVAILABLE','ROSTER_'+roster_status
    if rows.empty:return 'UNKNOWN','No fixture-specific availability evidence'
    status=set(rows.status)
    bad=status&UNAVAILABLE
    if bad:return 'UNAVAILABLE','; '.join(sorted(bad))
    uncertain=rows[rows.status.isin(UNCERTAIN)]
    clear=rows[rows.status.eq('NO_GAME_DESIGNATION')]
    if len(uncertain):
        # Only an explicit later designation clearance counts; an absent row does not.
        def effective(row):
            value=next((row.get(key) for key in ['modified_at_utc','published_at_utc','observed_at_utc'] if pd.notna(row.get(key)) and row.get(key)),None)
            return timestamp(value)
        latest=uncertain.apply(effective,axis=1).max()
        clear=clear[clear.apply(effective,axis=1)>latest] if len(clear) else clear
        if clear.empty:
            return 'UNCERTAIN','; '.join(sorted(set(uncertain.status)))
        if not clear.get('explicit_clearance',pd.Series(False,index=clear.index)).eq(True).any():return 'UNCERTAIN','Unresolved adverse designation'
    if status&{'NO_GAME_DESIGNATION','GAMEDAY_ACTIVE'}:return 'EXPECTED_ELIGIBLE','No adverse current designation; not a claim of health or starter confirmation'
    return 'UNKNOWN','No positive clearance evidence'


def reconcile_view(players,events,fixtures,asof,roster,coverage=None,starter_proof=None):
    """Create new columns only. Never mutate official player values or team scores."""
    events=validate_events(events,fixtures,asof);now=timestamp(asof);rows=[]
    if players.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate forecast player identity')
    if roster.duplicated(['team','gsis_id']).any():raise ValueError('Ambiguous current roster identity')
    lookup=fixtures.set_index('game_id');proof=starter_proof or {};coverage=coverage or {}
    for p in players.to_dict('records'):
        f=lookup.loc[p['game_id']];reason=[];cutoff=timestamp(f.kickoff_utc)-pd.Timedelta(minutes=1)
        selected=events[events.game_id.eq(p['game_id'])&events.player_id.eq(p['player_id'])&events.team.eq(p['team'])] if len(events) else events
        r=roster[roster.gsis_id.eq(p['player_id'])&roster.team.eq(p['team'])]
        rs=str(r.iloc[0].status).upper() if len(r)==1 else 'ROSTER_IDENTITY_MISSING'
        state,why=resolution(selected,rs)
        c=coverage.get((p['game_id'],p['team']),{})
        if state=='UNKNOWN' and c.get('complete_final') and c.get('allow_absent_clearance',True) and not c.get('critical_identity_errors') and rs=='ACT':
            state='EXPECTED_ELIGIBLE';why='Not listed on complete current final report; not confirmed gameday active or healthy'
        if rs not in {'ACT',*UNAVAILABLE}:state='UNKNOWN';why='Current roster identity/status unresolved'
        if c.get('critical_identity_errors') and state!='UNAVAILABLE':state='UNKNOWN';why='Unresolved critical player identity in current official report'
        fixture_state=str(f.get('eligibility_state','AMBIGUOUS'))
        if fixture_state!='PRE_KICKOFF' or now>=cutoff:state='CUTOFF_BLOCKED';why='No revised pregame view after cutoff or non-pregame state'
        key=p['game_id']+'|'+p['player_id']+'|'+p['team'];starter=bool(proof.get(key,{}).get('expected_starter',False))
        active=bool(state=='EXPECTED_ELIGIBLE' and len(selected) and selected.status.eq('GAMEDAY_ACTIVE').any())
        row=dict(game_id=p['game_id'],player_id=p['player_id'],player_name=p['player_name'],team=p['team'],position=p['position'],
            original_availability=p.get('availability_status','UNKNOWN'),current_eligibility=state,availability_reason=why,
            expected_starter_evidence=starter,gameday_active_confirmed=active,actionable_current=state=='EXPECTED_ELIGIBLE' and starter,
            original_starter_presentation=starter,view_at_utc=now.isoformat(),cutoff_utc=cutoff.isoformat(),
            source_evidence=selected.to_dict('records'),redistributed_opportunity=False,official_forecast_modified=False)
        for field in PROJECTIONS:
            row['original_'+field]=p.get(field)
            row['research_'+field]=0.0 if state=='UNAVAILABLE' else p.get(field) if state=='EXPECTED_ELIGIBLE' else None
        rows.append(row)
    return pd.DataFrame(rows)


def apply_current_sources(result,events,coverage,schedule,asof):
    """Used by full/partial future staging, before QB and player gates."""
    from dataclasses import replace
    events=validate_events(events,schedule,asof)
    teams=set(schedule.home_team)|set(schedule.away_team)
    records=result.records[result.records.team.isin(teams)].copy()
    states=result.coverage[result.coverage.team.isin(teams)].copy();events=events.copy()
    practice_events=events[events.get('report_stage',pd.Series('',index=events.index)).eq('PRACTICE')].copy()
    # Most restrictive corroborated status wins, regardless of retrieval order.
    if len(events):
        priority={s:3 for s in UNAVAILABLE};priority.update({s:2 for s in UNCERTAIN});priority['NO_GAME_DESIGNATION']=1
        events['_priority']=events.status.map(priority).fillna(0)
        events=events.sort_values('_priority').drop_duplicates(['game_id','team','player_id'],keep='last')
    for r in events.to_dict('records'):
        if r['status'] not in UNAVAILABLE|UNCERTAIN|{'NO_GAME_DESIGNATION'}:continue
        status=r['status'] if r['status']!='NO_GAME_DESIGNATION' else 'UNKNOWN'
        mask=records.get('record_type',pd.Series('',index=records.index)).eq('PLAYER_REPORT')&records.team.eq(r['team'])&records.player_id.eq(r['player_id'])
        existing=records[mask]
        if len(existing) and existing.game_status.isin(UNAVAILABLE).any() and status not in UNAVAILABLE:continue
        if len(existing) and existing.game_status.isin(UNCERTAIN).any() and r['status']=='NO_GAME_DESIGNATION':
            adverse=existing[existing.game_status.isin(UNCERTAIN)]
            effective=timestamp(r.get('modified_at_utc') or r.get('published_at_utc') or r['observed_at_utc'])
            if not r.get('explicit_clearance') or 'known_at_utc' not in adverse or effective<=adverse.known_at_utc.map(timestamp).max():continue
        new={**(existing.iloc[-1].to_dict() if len(existing) else {}),'team':r['team'],'player_id':r['player_id'],'player_name':r['player_name'],
            'season':int(schedule.iloc[0].season),'week':int(schedule.iloc[0].week),'game_status':status,'availability_status':status,
            'record_type':'PLAYER_REPORT','source':r['source_url'],'source_state':'VALID','coverage_status':'REPORT_PUBLISHED',
            'known_at_utc':r['known_at_utc'],'retrieved_at_utc':r['observed_at_utc'],'source_published_at_utc':r.get('published_at_utc'),
            'source_sha256':r['source_sha256'],'report_stage':r.get('report_stage','FINAL'),'explicit_no_game_designation':r['status']=='NO_GAME_DESIGNATION'}
        records=records.loc[~mask];records=pd.concat([records,pd.DataFrame([new])],ignore_index=True)
    practice_records=practice_events.sort_values('known_at_utc').to_dict('records') if len(practice_events) else []
    for r in practice_records:
        mask=records.get('record_type',pd.Series('',index=records.index)).eq('PLAYER_REPORT')&records.team.eq(r['team'])&records.player_id.eq(r['player_id'])
        existing=records[mask]
        old=existing.iloc[-1].to_dict() if len(existing) else {}
        status=old.get('game_status','UNKNOWN')
        if pd.isna(status):status='UNKNOWN'
        new={**old,'team':r['team'],'player_id':r['player_id'],'player_name':r['player_name'],'position':r['position'],
            'season':int(schedule.iloc[0].season),'week':int(schedule.iloc[0].week),'game_status':status,'availability_status':status,
            'record_type':'PLAYER_REPORT','source':old.get('source',r['source_url']),'source_state':'VALID',
            'coverage_status':'REPORT_PUBLISHED_PRACTICE','known_at_utc':old.get('known_at_utc',r['known_at_utc']),
            'retrieved_at_utc':r['observed_at_utc'],'report_stage':'PRACTICE',
            'practice_status_normalized':r['practice_status_normalized'],'practice_estimated':r['practice_estimated'],
            'practice_date':r['practice_date'],'practice_source_url':r['source_url'],'practice_source_sha256':r['source_sha256'],
            'practice_published_at_utc':r['published_at_utc'],'practice_modified_at_utc':r['modified_at_utc'],
            'practice_observed_at_utc':r['observed_at_utc'],'explicit_no_game_designation':False}
        records=records.loc[~mask];records=pd.concat([records,pd.DataFrame([new])],ignore_index=True)
    for i,r in states.iterrows():
        games=schedule[(schedule.home_team.eq(r.team))|schedule.away_team.eq(r.team)]
        if pd.notna(r.get('game_id')):games=games[games.game_id.eq(r['game_id'])]
        if len(games)!=1:raise ValueError('Availability schedule team ambiguity')
        g=games.iloc[0];c=coverage.get((g.game_id,r.team),{})
        block=records.get('record_type',pd.Series('',index=records.index)).eq('PLAYER_REPORT')&records.team.eq(r.team)
        statuses=records.loc[block,'game_status']
        empty_marker=r.get('report_empty',False)
        explicit=(len(statuses)>0 and statuses.isin(UNAVAILABLE|UNCERTAIN|{'ACTIVE','HEALTHY'}).all()) or (pd.notna(empty_marker) and bool(empty_marker))
        # Published practice coverage is insufficient once final game designations are due.
        practice_only=c.get('complete_practice') and not c.get('complete_final')
        missing=timestamp(asof)>=final_report_due(g.kickoff_utc) and (practice_only or not (explicit or c.get('complete_final')))
        if c.get('critical_identity_errors'):
            states.loc[i,'source_state']='SOURCE_MISSING';states.loc[i,'coverage_status']='IDENTITY_RECONCILIATION_FAILED'
        elif missing:
            states.loc[i,'source_state']='SOURCE_MISSING';states.loc[i,'coverage_status']='FINAL_DESIGNATIONS_MISSING'
        elif c.get('complete_final'):
            states.loc[i,'source_state']='VALID';states.loc[i,'coverage_status']='REPORT_PUBLISHED'
        elif c.get('complete_practice'):
            states.loc[i,'source_state']='VALID';states.loc[i,'coverage_status']='REPORT_PUBLISHED_PRACTICE'
            states.loc[i,'latest_report_known_at_utc']=c['observed_at_utc']
        states.loc[i,'final_designations_due_utc']=final_report_due(g.kickoff_utc).isoformat()
        states.loc[i,'complete_final_club_report']=bool(c.get('complete_final'))
        states.loc[i,'complete_practice_club_report']=bool(c.get('complete_practice'))
        records.loc[records.team.eq(r.team),'source_state']=states.loc[i,'source_state']
    for team in states.team:
        mask=records.get('record_type',pd.Series('',index=records.index)).eq('TEAM_COVERAGE')&records.team.eq(team)
        for field in ['source_state','coverage_status','final_designations_due_utc','complete_final_club_report','complete_practice_club_report']:
            records.loc[mask,field]=states.loc[states.team.eq(team),field].iloc[0]
    blocked=states.source_state.isin(['SOURCE_MISSING','ERROR','STALE']).any()
    warnings=list(result.warnings)
    if states.coverage_status.eq('FINAL_DESIGNATIONS_MISSING').any():warnings.append('Final game-designation evidence missing; practice-only coverage cannot certify availability')
    if states.coverage_status.eq('IDENTITY_RECONCILIATION_FAILED').any():warnings.append('Official report player/position identity unresolved; coverage remains blocked independently of report deadline')
    status='BLOCKED' if blocked else 'READY_WITH_WARNINGS' if states.source_state.eq('NOT_YET_PUBLISHED').any() else 'READY'
    latest=max([timestamp(result.source_known_at_utc),*[timestamp(v) for v in events.known_at_utc]]) if len(events) else timestamp(result.source_known_at_utc)
    urls=sorted(set(events.source_url)) if len(events) else []
    return replace(result,records=records,coverage=states,status=status,warnings=warnings,
        source_known_at_utc=latest.isoformat(),source_url='; '.join([result.source_url,*urls]),
        source_known_at_basis=result.source_known_at_basis+'; club publication/version and actual observation retained per row')
