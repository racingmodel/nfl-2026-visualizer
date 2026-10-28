from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import copy,hashlib,hmac,json,math,os,re,uuid

SCHEMA='weekly-prospective-research-v1'
TEAM_SHA='3c31726c7ca9bea5a4683128b5b0befe0f7d4635608a1d6e7ac43af8b2268fec'
PLAYER_SHA='397b4353d921511f29252f28851578285ed6b6b82208a5c0bdc0da6ace171d3a'
FEATURES=['home_pf8','home_pa8','away_pf8','away_pa8','home_history_n','away_history_n','home_advantage']
ADVERSE={'OUT','IR','PUP','INACTIVE','WAIVED','RELEASED','PRACTICE_SQUAD'}

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('utf-8')
def digest(x):return hashlib.sha256(canonical(x)).hexdigest()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def utc(t):
    t=datetime.fromisoformat(str(t).replace('Z','+00:00')) if not isinstance(t,datetime) else t
    if t.tzinfo is None:raise ValueError('Explicit UTC clock required')
    return t.astimezone(timezone.utc)
def clock():return datetime.now(timezone.utc)
def ident(s):
    if not isinstance(s,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',s):raise ValueError('Unsafe identity')
    return s
def atomic(path,body):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex);tmp.write_bytes(body);os.replace(tmp,path)
def immutable(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);b=canonical(obj)
    if path.exists():
        if path.read_bytes()!=b:raise ValueError('Immutable object differs')
    else:
        with path.open('xb') as f:f.write(b)
    return digest(obj)

def due(now,games,last_receipt=None):
    """UTC polling with explicit UK conversion, delay catch-up and bounded cadence."""
    now=utc(now);local=now.astimezone(ZoneInfo('Europe/London'))
    remaining=[utc(g['cutoff_utc'])-now for g in games if g.get('state') not in {'FINAL','LIVE','AMBIGUOUS'} and utc(g['cutoff_utc'])>now]
    if last_receipt is None:return True,'INITIAL_OR_RECOVERY'
    last=utc(last_receipt)
    if now<last:raise ValueError('Execution clock reversed')
    if any(g.get('state')=='PRE_KICKOFF' and now>=utc(g['cutoff_utc']) for g in games):return True,'CUTOFF_CLOSURE_DUE'
    if any(x<=timedelta(hours=2) for x in remaining) and now-last>=timedelta(minutes=15):return True,'BOUNDED_GAME_DAY_POLL'
    if games and now-last>=timedelta(minutes=15):
        from nfl2026.live.availability import final_report_due
        for g in games:
            report_due=utc(final_report_due(utc(g.get('kickoff_utc') or (utc(g['cutoff_utc'])+timedelta(minutes=1)))))
            if report_due<=now<report_due+timedelta(hours=1):return True,'BOUNDED_FINAL_REPORT_PUBLICATION_WINDOW'
    if any(g.get('state') in {'LOCKED','LIVE'} and utc(g['cutoff_utc'])<=now<utc(g['cutoff_utc'])+timedelta(hours=6) for g in games) and now-last>=timedelta(minutes=30):return True,'BOUNDED_POSTGAME_RESULT_POLL'
    # Scheduled08/12/16/20 UK runs delayed into the same hour still execute once.
    slot=local.replace(minute=0,second=0,microsecond=0)
    if local.hour in {8,12,16,20} and last<slot.astimezone(timezone.utc):return True,'UK_SCHEDULE_SLOT'
    today=[local.replace(hour=h,minute=0,second=0,microsecond=0) for h in [8,12,16,20]]
    previous=(local-timedelta(days=1)).replace(hour=20,minute=0,second=0,microsecond=0)
    latest=max([s for s in today if s<=local]+[previous]).astimezone(timezone.utc)
    if last<latest:return True,'MISSED_UK_SLOT_CATCHUP'
    return False,'NOT_DUE'

def verify_packet(packet,now):
    now=utc(now)
    if packet.get('schema')!='weekly-source-packet-v1':raise ValueError('Unsupported source packet')
    if not utc(packet['observed_at_utc'])<=now or now-utc(packet['observed_at_utc'])>timedelta(minutes=15):raise ValueError('Independent schedule stale/future')
    if packet.get('model_sha256')!=TEAM_SHA:raise ValueError('Unapproved model change')
    if packet.get('player_model_sha256')!=PLAYER_SHA:raise ValueError('Unapproved player model change')
    games=packet['games'];ids=[g['game_id'] for g in games]
    if len(ids)!=len(set(ids)) or not games:raise ValueError('Fixture identity population invalid')
    for g in games:
        gid=ident(g['game_id']);expected=f"{int(packet['season'])}_{int(packet['week']):02d}_{g['away_team']}_{g['home_team']}"
        if gid!=expected or g['away_team']==g['home_team']:raise ValueError('Wrong team/opponent/game join')
        if utc(g['cutoff_utc'])!=utc(g['kickoff_utc'])-timedelta(minutes=1):raise ValueError('Cutoff mismatch')
        if g['state'] not in {'PRE_KICKOFF','LIVE','FINAL','AMBIGUOUS'}:raise ValueError('Unknown fixture state')
    for row in packet.get('evidence',[]):
        if row['game_id'] not in ids:raise ValueError('Evidence fixture differs')
        if row.get('validation')!='EXACT_SOURCE_IDENTITY_VALIDATED':raise ValueError('Unvalidated source evidence')
        observed=utc(row['observed_at_utc']);known=utc(row.get('published_at_utc') or row['observed_at_utc']);modified=utc(row.get('modified_at_utc') or known)
        if not known<=modified<=observed<=now:raise ValueError('Evidence clocks invalid')
        game=next(g for g in games if g['game_id']==row['game_id'])
        if row['team'] not in {game['away_team'],game['home_team']}:raise ValueError('Wrong evidence team')
        if not re.fullmatch('[a-f0-9]{64}',row['source_sha256']):raise ValueError('Raw source hash absent')
        if row.get('type')=='AVAILABILITY' and row['status'] not in ADVERSE|{'UNKNOWN','QUESTIONABLE','DOUBTFUL','VERIFIED_ACTIVE'}:raise ValueError('Unsupported availability')
        if row.get('status') in {'INACTIVE','VERIFIED_ACTIVE'} and row.get('strict_own_list_certificate') is not True:raise ValueError('Strict own-list proof required; production partial parser forbidden')
        if row.get('type')=='AVAILABILITY':
            from urllib.parse import urlparse
            from nfl2026.live.providers.qb_evidence import CLUB_HOSTS
            host=urlparse(str(row.get('source_url','')))
            if host.scheme!='https' or host.hostname not in {'www.nfl.com','nfl.com','www.'+CLUB_HOSTS[row['team']],CLUB_HOSTS[row['team']]}:raise ValueError('Availability source is not authoritative own club/NFL HTTPS')
    return packet

def semantic_context(rows):
    fields=['type','team','player_id','name','status','expected_qb_id','expected_qb_name','medical_status','role','roster_status','source_url','published_at_utc','modified_at_utc','strict_own_list_certificate']
    return sorted([{k:r[k] for k in fields if k in r} for r in rows],key=canonical)

def availability(previous,rows):
    result=copy.deepcopy(previous)
    for r in rows:
        if r.get('type')!='AVAILABILITY':continue
        key=r['team']+'|'+r['player_id'];prior=result.get(key,{'status':'UNKNOWN','gameday_active_confirmed':False})
        status=r['status']
        # A later UNKNOWN cannot erase explicit adverse information. A positive
        # active report remains separate from medical designation/history.
        if status=='UNKNOWN' and prior['status'] in ADVERSE|{'QUESTIONABLE','DOUBTFUL'}:continue
        if status=='VERIFIED_ACTIVE':
            result[key]=dict(prior,gameday_active_confirmed=True,active_source=r)
        else:result[key]=dict(status=status,gameday_active_confirmed=False,evidence=r)
    return result

class Store:
    def __init__(self,root,*,simulation=False):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.simulation=simulation
        mode=self.root/'STORE_MODE.json'
        if mode.exists() and load(mode)['simulation'] is not simulation:raise ValueError('Simulation/real store boundary differs')
        if not mode.exists():immutable(mode,dict(schema=SCHEMA,simulation=simulation))
    @contextmanager
    def lock(self):
        p=self.root/'.writer-lock'
        try:p.mkdir()
        except FileExistsError:raise ValueError('Another writer active; fail closed')
        try:yield
        finally:p.rmdir()
    def receipt(self,value):
        value=dict(value,actual_receipt_written_at_utc=clock().isoformat(),github_run_id=os.environ.get('GITHUB_RUN_ID'),github_run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'));rid=clock().strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:6];immutable(self.root/'receipts'/(rid+'.json'),value);return value
    def head(self):
        p=self.root/'HEAD.json'
        if not p.exists():return None,{}
        ref=load(p);c=load(self.root/'commits'/(ident(ref['commit_id'])+'.json'))
        if digest(c)!=ref['sha256']:raise ValueError('Selected commit hash differs')
        result={}
        for gid,rid in c['selection'].items():
            r=load(self.root/'revisions'/ident(gid)/(ident(rid)+'.json'))
            if digest(r)!=''+rid.split('rev-')[-1]:raise ValueError('Revision hash differs')
            result[gid]=r
        return ref,result
    def _commit(self,selection,parent,now,reason,source_hash):
        c=dict(schema=SCHEMA,selection=selection,parent_commit=parent,created_at_utc=utc(now).isoformat(),reason=reason,accepted_source_snapshot_sha256=source_hash,official=False)
        cid='commit-'+digest(c);immutable(self.root/'commits'/(cid+'.json'),c);atomic(self.root/'HEAD.json',canonical(dict(commit_id=cid,sha256=digest(c))));return cid
    def initialise(self,seed,now,source_identity):
        """Import original frozen bytes/identities, never re-infer existing originals."""
        with self.lock():
            if (self.root/'HEAD.json').exists():raise ValueError('Week already initialised; use refresh')
            selected={};fixture_ids=set()
            for g in seed['games']:
                gid=ident(g['game_id']);fixture_ids.add(gid)
                forecast={k:g.get(k) for k in ['away_expected_points','home_expected_points','model_margin','model_total']}
                if any(v is not None and (not isinstance(v,(float,int)) or not math.isfinite(v)) for v in forecast.values()):raise ValueError('Invalid original numeric value')
                r=dict(game_id=gid,away_team=g['away_team'],home_team=g['home_team'],kickoff_utc=g['kickoff_utc'],cutoff_utc=g['cutoff_utc'],state='PRE_KICKOFF',stage='EARLY_FORECAST',created_at_utc=g.get('created_at_utc') or utc(now).isoformat(),forecast=forecast,players=[p for p in seed.get('players',[]) if p['game_id']==gid],quarterbacks=g.get('expected_QBs',[]),availability={},original_source_identity=source_identity,original_freeze_id=g.get('freeze_id'),parent_revision_id=None,reason='IMMUTABLE_ORIGINAL_RESEARCH_IMPORT',model_sha256=TEAM_SHA,player_model_sha256=PLAYER_SHA,model_certification='NOT_PRODUCTION_CERTIFIED',actionable=False,component_contract='INDEPENDENT_PARTICIPATION_CONDITIONAL_MARGINALS_NO_TD_OR_TEAM_SCORE_PLAYER_INPUT',team_input_hash=g.get('team_input_hash'),player_input_hash=g.get('player_input_hash'),context_hash=None,accepted_sources=[],numeric_change=False)
                r.update(model_id='causal-score-baseline-v1-3c31726c7ca9bea5',feature_contract='causal-score-history8-v1',research_runtime_contract=SCHEMA,simulation=self.simulation)
                rid='rev-'+digest(r);immutable(self.root/'revisions'/gid/(rid+'.json'),r);immutable(self.root/'originals'/(gid+'.json'),dict(revision_id=rid,sha256=digest(r)));selected[gid]=rid
            if len(fixture_ids)!=len(seed['games']):raise ValueError('Duplicate original fixtures')
            cid=self._commit(selected,None,now,'INITIAL_WEEK_FORECAST_IMPORT',source_identity)
            return self.receipt(dict(status='INITIAL_WEEK_FORECAST',commit_id=cid,fixtures=len(selected),numeric_inference_calls=0,official=False))
    def refresh(self,packet,now,infer,*,mode='REFRESH_WEEK_FORECASTS',fail_before_commit=False):
        """Source-only changes never call inference. Selected revisions are atomic."""
        try:
            verify_packet(packet,now)
            if bool(packet.get('simulation',False)) is not self.simulation:raise ValueError('Simulated source packet cannot enter real store')
            with self.lock():
                immutable(self.root/'source_packets'/(digest(packet)+'.json'),packet)
                ref,current=self.head()
                if not ref:raise ValueError('Initial forecast import required')
                if set(current)!={g['game_id'] for g in packet['games']}:raise ValueError('Fixture set drift')
                selected={g:'rev-'+digest(r) for g,r in current.items()};changed=[];calls=0
                for g in packet['games']:
                    gid=g['game_id'];old=current[gid];closed=utc(now)>=utc(old['cutoff_utc']) or old['state'] in {'LOCKED','LIVE','FINAL','AMBIGUOUS'} or g['state']!='PRE_KICKOFF'
                    if (old['away_team'],old['home_team'],utc(old['kickoff_utc']))!=(g['away_team'],g['home_team'],utc(g['kickoff_utc'])):raise ValueError('Fixture identity changed; manual review required')
                    if closed:
                        # The latest accepted pregame revision stays selected inside
                        # a distinct lifecycle record; no postgame inference/overlay.
                        target='FINAL' if g['state']=='FINAL' else 'LOCKED'
                        if old['state'] in {'LOCKED','FINAL'} and (target!='FINAL' or old['state']=='FINAL'):continue
                        new=copy.deepcopy(old);new.update(state=target,stage=target,parent_revision_id=selected[gid],created_at_utc=utc(now).isoformat(),reason='ABSOLUTE_CUTOFF_OR_OBSERVED_STATE_CLOSURE',numeric_change=False)
                        if target=='FINAL':
                            a,h=g.get('actual_away_score'),g.get('actual_home_score')
                            if any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in [a,h]):raise ValueError('FINAL labels unresolved')
                            original=load(self.root/'revisions'/gid/(load(self.root/'originals'/(gid+'.json'))['revision_id']+'.json'))
                            new['actuals']=dict(away_score=a,home_score=h,observed_at_utc=packet['observed_at_utc'],schedule_sha256=packet['schedule_sha256'])
                            def errors(r):
                                f=r['forecast'];return None if f['home_expected_points'] is None else dict(team_MAE=(abs(f['home_expected_points']-h)+abs(f['away_expected_points']-a))/2,margin_error=abs((f['home_expected_points']-f['away_expected_points'])-(h-a)))
                            new['evaluation']=dict(original=errors(original),latest_pregame=errors(old),missing_player_labels_remain_unknown=True)
                    else:
                        rows=[r for r in packet.get('evidence',[]) if r['game_id']==gid and utc(r['observed_at_utc'])<utc(g['cutoff_utc'])]
                        av=availability(old['availability'],rows);ctx=digest(semantic_context(rows));x=packet.get('team_inputs',{}).get(gid);px=packet.get('player_inputs',{}).get(gid)
                        xhash=digest(x) if x is not None else None;phash=digest(px) if px is not None else None
                        # An imported first issue must be bound to its genuine
                        # original inputs before refresh, not recomputed by default.
                        consume_team=x is not None and xhash!=old.get('team_input_hash')
                        consume_player=px is not None and phash!=old.get('player_input_hash')
                        new=copy.deepcopy(old);reason=[]
                        if consume_team or consume_player:
                            values=infer(g,x if consume_team else None,px if consume_player else None,packet,now);calls+=1
                            if consume_team:
                                h,a=values['home_expected_points'],values['away_expected_points']
                                if any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=60 for v in [a,h]):raise ValueError('Invalid model output')
                                new['forecast']=dict(away_expected_points=a,home_expected_points=h,model_total=a+h,model_margin=h-a);new['team_input_hash']=xhash;reason.append('CONSUMED_TEAM_HISTORY_CHANGED')
                            if consume_player:new['players']=values['players'];new['player_input_hash']=phash;reason.append('CONSUMED_PLAYER_USAGE_CHANGED')
                        if ctx!=old.get('context_hash') or av!=old['availability']:
                            new['availability']=av;new['accepted_sources']=rows;new['context_hash']=ctx;new['quarterbacks']=[r for r in rows if r.get('type')=='QB'] or old['quarterbacks'];reason.append('PERSONNEL_CONTEXT_UPDATED_NO_INJURY_SCORE_ADJUSTMENT')
                        if not reason:continue
                        if all(r.get('strict_own_list_certificate') is True for r in rows if r.get('type')=='OWN_LIST') and {r['team'] for r in rows if r.get('type')=='OWN_LIST'}=={g['away_team'],g['home_team']}:stage='PREGAME_FORECAST'
                        else:stage='UPDATED_FORECAST'
                        if old['forecast']['home_expected_points'] is None and new['forecast']['home_expected_points'] is not None:stage='EARLY_FORECAST';reason.insert(0,'FIRST_QB_NEUTRAL_RESEARCH_SCORE_NOT_UNSUPPORTED_STARTER_SELECTION')
                        new.update(parent_revision_id=selected[gid],created_at_utc=utc(now).isoformat(),stage=stage,reason=';'.join(reason),numeric_change=new['forecast']!=old['forecast'] or new['players']!=old['players'])
                    rid='rev-'+digest(new);immutable(self.root/'revisions'/gid/(rid+'.json'),new);selected[gid]=rid;changed.append(gid)
                if fail_before_commit:raise ValueError('Injected isolated release failure')
                guard=max(utc(now),clock());discarded=[]
                for gid,old in current.items():
                    proposed=load(self.root/'revisions'/gid/(selected[gid]+'.json'))
                    if guard>=utc(old['cutoff_utc']) and proposed['state']=='PRE_KICKOFF':
                        # Never admit an update merely because processing began
                        # before cutoff. Keep the previously selected forecast.
                        discarded.append(selected[gid]);locked=copy.deepcopy(old);locked.update(state='LOCKED',stage='LOCKED',reason='CUTOFF_CROSSED_DURING_EXECUTION_PRIOR_SELECTION_RETAINED',created_at_utc=guard.isoformat(),parent_revision_id='rev-'+digest(old),numeric_change=False);rid='rev-'+digest(locked);immutable(self.root/'revisions'/gid/(rid+'.json'),locked);selected[gid]=rid
                        if gid not in changed:changed.append(gid)
                cid=self._commit(selected,ref['commit_id'],now,mode,digest(packet)) if changed else ref['commit_id']
                return self.receipt(dict(status='UPDATED' if changed else 'UNCHANGED',commit_id=cid,affected_fixtures=changed,numeric_inference_calls=calls,unselected_cutoff_candidates=discarded,accepted_snapshot_sha256=digest(packet),observed_at_utc=packet['observed_at_utc'],official=False))
        except Exception as exc:
            self.receipt(dict(status='FAILED_LAST_KNOWN_GOOD_RETAINED',error=str(exc),official=False));raise
    def view(self,now):
        ref,selection=self.head();games=[]
        for gid,r in selection.items():
            original_id=load(self.root/'originals'/(gid+'.json'))['revision_id'];original=load(self.root/'revisions'/gid/(original_id+'.json'));trace=[];cursor=r
            while True:
                trace.append(dict(revision_id='rev-'+digest(cursor),created_at_utc=cursor['created_at_utc'],reason=cursor['reason'],numeric_change=cursor['numeric_change']))
                if not cursor['parent_revision_id']:break
                cursor=load(self.root/'revisions'/gid/(cursor['parent_revision_id']+'.json'))
            if original['forecast']['home_expected_points'] is None:
                for event in reversed(trace):
                    candidate=load(self.root/'revisions'/gid/(event['revision_id']+'.json'))
                    if candidate['forecast']['home_expected_points'] is not None:original=candidate;break
            games.append(dict(r,revision_id='rev-'+digest(r),original_forecast=original['forecast'],original_created_at_utc=original['created_at_utc'],audit=trace,runtime_closed=utc(now)>=utc(r['cutoff_utc']),actionable=False))
        checks=[load(p) for p in (self.root/'receipts').glob('*.json')]
        last_check=max((c['observed_at_utc'] for c in checks if c.get('status') in {'UPDATED','UNCHANGED'} and c.get('observed_at_utc')),default=None)
        return dict(schema=SCHEMA,commit=ref,generated_at_utc=utc(now).isoformat(),last_checked_at_utc=last_check,games=games,model_certification='NOT_PRODUCTION_CERTIFIED',publication=False)

def validate_publication(policy,approval,key,now,site,payload):
    """One-time exact policy approval, never self-approved by source jobs."""
    if not approval or approval.get('decision')!='APPROVED':raise ValueError('Explicit publication approval absent')
    expected=os.environ.get('RESEARCH_APPROVED_POLICY_SHA256','')
    if expected!=digest(policy) or approval.get('policy_sha256')!=expected:raise ValueError('Environment policy pin differs')
    if not key or not hmac.compare_digest(hmac.new(key.encode(),canonical(approval['grant']),hashlib.sha256).hexdigest(),approval.get('signature','')):raise ValueError('Publication authority signature absent')
    grant=approval['grant']
    if grant['policy_sha256']!=expected or not utc(grant['approved_at_utc'])<=utc(now)<utc(grant['expires_at_utc']):raise ValueError('Approval future/expired')
    if policy['model_hashes']!={'team':TEAM_SHA,'player':PLAYER_SHA} or policy.get('official_replacement') or policy.get('paid_requests'):raise ValueError('Publication scope invalid')
    for n,h in policy['static_assets'].items():
        p=(Path(site)/n).resolve()
        if not p.is_relative_to(Path(site).resolve()) or not p.is_file() or sha(p)!=h:raise ValueError('UI/static publication scope expanded')
    actual={str(p.relative_to(site)).replace('\\','/') for p in Path(site).rglob('*') if p.is_file()}
    if actual-set(policy['static_assets'])-{'data/weekly_research.json','data/weekly_research.sha256','release_receipt.json'}:raise ValueError('Unexpected public asset')
    if payload['schema']!=SCHEMA or any(g.get('simulation') is True or g.get('actionable') is not False or g['model_certification']!='NOT_PRODUCTION_CERTIFIED' for g in payload['games']):raise ValueError('Research label/scope changed or simulated fixture')
    for g in payload['games']:
        season,week,*_=g['game_id'].split('_')
        if int(season)!=policy['season'] or not policy['first_week']<=int(week)<=policy['last_week']:raise ValueError('Unauthorised weekly scope')
        if g.get('model_sha256')!=TEAM_SHA or g.get('player_model_sha256')!=PLAYER_SHA:raise ValueError('Published model identity differs')
        if set(g['forecast'])!={'away_expected_points','home_expected_points','model_margin','model_total'}:raise ValueError('Uncertified numeric component added')
        if any(p.get('anytime_td_probability') is not None or p.get('passing_yards') is not None or p.get('quarter_scores') is not None for p in g['players']):raise ValueError('Uncertified player extension added')
    return True

def validate_rollback(previous,current,now):
    before={g['game_id']:g for g in previous['games']};after={g['game_id']:g for g in current['games']}
    if set(before)!=set(after):raise ValueError('Rollback cannot change fixture scope')
    for gid,g in after.items():
        if utc(now)>=utc(g['cutoff_utc']) and (g['forecast']!=before[gid]['forecast'] or g['players']!=before[gid]['players']):raise ValueError('Rollback would revise locked numerical forecasts')
    return True
