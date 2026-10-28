"""Research-only source/snapshot contracts. Never calls production generation."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json
import numpy as np
import pandas as pd
from nfl2026.live.availability import final_report_due
from nfl2026.features.player_advanced import PLAYER_FEATURES

TEAM_ID='causal-score-baseline-v1-3c31726c7ca9bea5'
TEAM_SHA='3c31726c7ca9bea5a4683128b5b0befe0f7d4635608a1d6e7ac43af8b2268fec'
PLAYER_SHA='397b4353d921511f29252f28851578285ed6b6b82208a5c0bdc0da6ace171d3a'
TARGETS=['carries','targets','rushing_yards','receiving_yards']
def utc(v):
    t=pd.Timestamp(v)
    if pd.isna(t) or t.tzinfo is None:raise ValueError('Explicit nonmissing UTC clock required')
    return t.tz_convert('UTC')
def true(v):return v is True or str(v).lower() in {'true','1'}
def assess_fixture(game,states,qbs,coverage,roster,now,schedule_observed):
    """Provisional research eligibility is distinct from certification or health."""
    now=utc(now);observed=utc(schedule_observed);kick=utc(game['kickoff_utc']);gid=game['game_id'];sides={game['away_team'],game['home_team']};reasons=[]
    if gid!=f"{int(game['season'])}_{int(game['week']):02d}_{game['away_team']}_{game['home_team']}" or len(sides)!=2:raise ValueError('Fixture identity mismatch')
    if not observed<=now or now-observed>pd.Timedelta(minutes=15):reasons.append('INDEPENDENT_SCHEDULE_STALE_OR_FUTURE')
    s=states[states.game_id.eq(gid)]
    if len(s)!=1:reasons.append('INDEPENDENT_FIXTURE_MISSING_OR_DUPLICATED')
    else:
        r=s.iloc[0]
        if r.away_team!=game['away_team'] or r.home_team!=game['home_team'] or utc(r.kickoff_utc)!=kick:reasons.append('INDEPENDENT_FIXTURE_IDENTITY_CHANGED')
        if r.eligibility_state!='PRE_KICKOFF':reasons.append('CLOSED_'+str(r.eligibility_state))
    if now>=kick-pd.Timedelta(minutes=1):reasons.append('CLOSED_ABSOLUTE_CUTOFF')
    q=qbs[qbs.game_id.eq(gid)];c=coverage[coverage.game_id.eq(gid)];context=[]
    for team in sorted(sides):
        one=q[q.team.eq(team)]
        if len(one)!=1:reasons.append(team+':QB_IDENTITY_MISSING_OR_DUPLICATED');continue
        row=one.iloc[0];p=roster[roster.team.eq(team)&roster.gsis_id.astype(str).eq(str(row.qb_player_id))]
        if row.source_state!='VALID' or row.status not in {'EXPECTED','CONFIRMED'} or not true(row.active_roster_match):reasons.append(team+':QB_EXPECTATION_UNRESOLVED')
        if len(p)!=1 or p.iloc[0].position!='QB' or p.iloc[0].full_name!=row.qb_name or p.iloc[0].status!='ACT':reasons.append(team+':QB_ROSTER_IDENTITY_INVALID')
        elif str(p.iloc[0].get('transaction_reconciliation_error','')) or str(p.iloc[0].get('current_transaction_state','')) in {'IR','RELEASED','PRACTICE_SQUAD','CONFLICT','INACTIVE'}:reasons.append(team+':QB_TRANSACTION_UNRESOLVED')
        if row.injury_status in {'OUT','IR','PUP','INACTIVE','DOUBTFUL'}:reasons.append(team+':QB_ADVERSE_STATUS')
        try:
            known=utc(row.known_at_utc);retrieved=utc(row.retrieved_at_utc)
            if not known<=retrieved<=now or now-known>pd.Timedelta(hours=36):reasons.append(team+':QB_SOURCE_CLOCK_INVALID')
        except (ValueError,TypeError):reasons.append(team+':QB_SOURCE_CLOCK_MISSING')
        context.append(dict(team=team,player_id=str(row.qb_player_id),name=row.qb_name,status=row.status,medical_status=row.injury_status,starter_confirmed=true(row.starter_confirmed),gameday_active_confirmed=False,source_state=row.source_state,known_at_utc=row.known_at_utc,retrieved_at_utc=row.retrieved_at_utc))
        cv=c[c.team.eq(team)]
        if len(cv)!=1:reasons.append(team+':INJURY_COVERAGE_MISSING_OR_DUPLICATED');continue
        x=cv.iloc[0]
        if now>=final_report_due(kick):
            if x.source_state!='VALID' or not true(x.complete_final_club_report):reasons.append(team+':FINAL_REPORT_REQUIRED')
        elif x.source_state=='VALID' and (true(x.complete_practice_club_report) or true(x.complete_final_club_report)):pass
        elif x.source_state=='VALID' and x.get('coverage_status')=='REPORT_PUBLISHED' and pd.to_numeric(x.get('records',0),errors='coerce')>0 and not true(x.get('report_empty',True)) and pd.to_numeric(x.get('unresolved_player_ids',1),errors='coerce')==0:
            # A real nonempty, exact-identity structured PRACTICE report can
            # support provisional research. Empty coverage never does, and
            # this branch is unreachable after the required final deadline.
            pass
        elif x.source_state=='NOT_YET_PUBLISHED':
            try:
                if now>=utc(x.first_report_due_utc):reasons.append(team+':FIRST_REPORT_OVERDUE')
            except (ValueError,TypeError):reasons.append(team+':FIRST_REPORT_CLOCK_MISSING')
        else:reasons.append(team+':CURRENT_INJURY_SOURCE_INVALID')
    if set(q.team)!=sides or len(q)!=2 or set(c.team)!=sides or len(c)!=2:reasons.append('PAIRED_SOURCE_POPULATION_INVALID')
    return dict(game_id=gid,eligible=not reasons,reasons=sorted(set(reasons)),stage='PROVISIONAL_PROSPECTIVE_RESEARCH_ONLY',quarterbacks=context,official_eligible=False,actionable=False,cutoff_utc=(kick-pd.Timedelta(minutes=1)).isoformat())

def player_contract(payload,frame,fixtures):
    """Independent marginal usage model, not a new joint/TD-budget contract."""
    if payload.get('features')!=PLAYER_FEATURES or set(payload.get('models',{}))!=set(TARGETS):raise ValueError('Exact23-feature/four-target raw player contract required')
    if any(isinstance(payload['models'][t],dict) or not hasattr(payload['models'][t],'predict') for t in TARGETS):raise ValueError('Unsupported fallback estimator')
    if frame.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate player identity')
    lookup=fixtures.set_index('game_id')
    for r in frame.itertuples():
        if r.game_id not in lookup.index:raise ValueError('Player fixture has no research parent')
        f=lookup.loc[r.game_id]
        if (r.team,r.opponent) not in {(f.away_team,f.home_team),(f.home_team,f.away_team)} or r.position not in {'QB','RB','FB','WR','TE'}:raise ValueError('Player team/opponent/position identity differs')
        if utc(r.kickoff_utc)!=utc(f.kickoff_utc):raise ValueError('Player kickoff differs')
    x=frame[PLAYER_FEATURES].apply(pd.to_numeric,errors='raise')
    if np.isinf(x.to_numpy(float)).any():raise ValueError('Infinite player input')
    if any(any(w in f for w in ['expected_points','team_score','model_total','bookmaker','odds']) for f in payload['features']):raise ValueError('Unbound team/market input')
    return x

def raw_player_predictions(payload,frame,fixtures):
    x=player_contract(payload,frame,fixtures)
    # Deterministic reductions in memory; fitted weights and artifacts unchanged.
    for model in payload['models'].values():
        for _,step in getattr(model,'steps',[]):
            if hasattr(step,'n_jobs'):step.n_jobs=1
    output=frame[['game_id','player_id','player_name','team','opponent','position','kickoff_utc']].copy()
    for target in TARGETS:
        values=np.asarray(payload['models'][target].predict(x),dtype=float)
        if values.shape!=(len(frame),) or not np.isfinite(values).all():raise ValueError('Invalid raw player output')
        output['research_'+target]=np.clip(values,0,None)
    output['interpretation']='CONDITIONAL_ON_PARTICIPATION_NOT_CLEARED_ACTIVE'
    output['actionable']=False
    return output

def verify_artifact(path,expected):
    actual=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if actual!=expected:raise ValueError('Fixed predeclared model identity changed')
    return actual

def issued_fixtures(catalog,root):
    """Separate research lineage; never an official release/model selector."""
    records={};root=Path(root).resolve()
    for p in Path(catalog).glob('*.json'):
        r=json.loads(p.read_text(encoding='utf-8'));freeze=(root/r['freeze_path']).resolve()
        if not freeze.is_relative_to(root) or not freeze.is_file() or verify_artifact(freeze,r['freeze_sha256'])!=r['freeze_sha256']:raise ValueError('Research issuance lineage differs')
        f=json.loads(freeze.read_text(encoding='utf-8'))
        if f['status']!='PROSPECTIVE_RESEARCH_NOT_OFFICIAL' or f['team_model_id']!=TEAM_ID or r['game_id'] not in f['fixtures'] or r['game_id'] in records:raise ValueError('Research first-issue identity differs')
        records[r['game_id']]=r
    return records

def register_first_issuance(catalog,root,freeze_path):
    root=Path(root).resolve();freeze_path=Path(freeze_path).resolve();f=json.loads(freeze_path.read_text(encoding='utf-8'));catalog=Path(catalog);catalog.mkdir(parents=True,exist_ok=True)
    if not freeze_path.is_relative_to(root) or f['status']!='PROSPECTIVE_RESEARCH_NOT_OFFICIAL' or f['team_model_id']!=TEAM_ID:raise ValueError('Invalid research freeze')
    existing=issued_fixtures(catalog,root);records=[]
    for gid in f['fixtures']:
        if Path(gid).name!=gid:raise ValueError('Unsafe fixture key')
        record=dict(game_id=gid,shadow_id=f['shadow_id'],freeze_id=f['freeze_id'],freeze_path=str(freeze_path.relative_to(root)),freeze_sha256=hashlib.sha256(freeze_path.read_bytes()).hexdigest(),frozen_at_utc=f['frozen_at_utc'],model_id=TEAM_ID,official=False,publication=False)
        if gid in existing and existing[gid]!=record:raise ValueError('First research forecast already issued; replacement forbidden')
        p=catalog/(gid+'.json')
        if not p.exists():
            with p.open('x',encoding='utf-8') as stream:json.dump(record,stream,indent=2,ensure_ascii=True)
        records.append(record)
    return records
