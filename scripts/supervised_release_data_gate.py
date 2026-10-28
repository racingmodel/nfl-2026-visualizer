"""Proposed secret-free public-data gate. No network, inference or deployment."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import hashlib,json,math,re
TEAM='3c31726c7ca9bea5a4683128b5b0befe0f7d4635608a1d6e7ac43af8b2268fec'
PLAYER='397b4353d921511f29252f28851578285ed6b6b82208a5c0bdc0da6ace171d3a'
PARENT='frontend-classic-week8-974591f0e1ca648d'
PURPOSE='ONE_SUPERVISED_HOSTED_WEEK8_PUBLICATION'
ADVERSE={'OUT','IR','PUP','INACTIVE','WAIVED','RELEASED','PRACTICE_SQUAD'}
def require(ok,msg):
 if not ok:raise ValueError(msg)
def utc(s):
 d=datetime.fromisoformat(s.replace('Z','+00:00'));require(d.tzinfo is not None,'UTC timezone missing');return d.astimezone(timezone.utc)
def sha(b):return hashlib.sha256(b).hexdigest()
def dynamic(n):return n in {'data/weekly_research.json','data/weekly_research.sha256','data/hosted_catalog.json','release_receipt.json'} or bool(re.fullmatch(r'data/research/2026-w(?:0[89]|1[0-8])\.json',n))
def records(data,previous,now):
 require(data['schema']=='weekly-prospective-research-v1' and data['model_certification']=='NOT_PRODUCTION_CERTIFIED','Research scope differs')
 old={g['game_id']:g for p in previous for g in p['games']};seen=set();ids=set()
 for g in data['games']:
  gid=g['game_id'];parts=gid.split('_');require(len(parts)==4 and parts[0]=='2026' and 8<=int(parts[1])<=18 and gid==f"2026_{int(parts[1]):02d}_{g['away_team']}_{g['home_team']}" and gid not in seen,'Fixture identity/scope differs');seen.add(gid)
  require(g.get('simulation') is not True and g['actionable'] is False and g['model_certification']=='NOT_PRODUCTION_CERTIFIED' and g['model_sha256']==TEAM and g['player_model_sha256']==PLAYER,'Model/actionability differs')
  require(set(g['forecast'])=={'home_expected_points','away_expected_points','model_margin','model_total'},'Unsupported numerical component')
  values=list(g['forecast'].values());require(all(v is None for v in values) or all(isinstance(v,(int,float)) and math.isfinite(v) for v in values),'Missing/invalid numerical values')
  if values[0] is not None:require(abs(g['forecast']['home_expected_points']+g['forecast']['away_expected_points']-g['forecast']['model_total'])<1e-8 and abs(g['forecast']['home_expected_points']-g['forecast']['away_expected_points']-g['forecast']['model_margin'])<1e-8,'Incoherent score')
  cutoff=utc(g['cutoff_utc']);require(cutoff==utc(g['kickoff_utc'])-timedelta(minutes=1),'Cutoff differs');require(utc(g['original_created_at_utc'])<cutoff and utc(g['created_at_utc'])<=now,'Creation clocks differ')
  if g['state']=='PRE_KICKOFF':require(utc(g['created_at_utc'])<cutoff,'Pregame view created after cutoff')
  if g['state']=='FINAL':
   actual=g.get('actuals',{});require(now>=utc(g['kickoff_utc']) and all(type(actual.get(k)) in {int,float} and math.isfinite(actual[k]) and actual[k]>=0 for k in ['home_score','away_score']) and utc(actual['observed_at_utc'])<=now and bool(re.fullmatch('[a-f0-9]{64}',actual.get('schedule_sha256',''))),'Final result provenance unresolved')
  for p in g['players']:
   key=(gid,p['player_id'],p['team']);require(key not in ids and p['game_id']==gid and p['team'] in [g['away_team'],g['home_team']] and p['opponent']==(g['home_team'] if p['team']==g['away_team'] else g['away_team']),'Player identity differs');ids.add(key)
   require(p['actionable'] is False and p['verified_active'] is False and p['model_sha256']==PLAYER and all(p.get(k) is None for k in ['anytime_td_probability','passing_yards','quarter_scores','model_fair_odds']),'Uncertified player component')
   require(all(isinstance(p[k],(int,float)) and math.isfinite(p[k]) and p[k]>=0 for k in ['research_rushing_yards','research_receiving_yards','research_carries','research_targets']),'Invalid player mean')
  for s in g.get('accepted_sources',[]):
   observed=utc(s['observed_at_utc']);published=utc(s.get('published_at_utc') or s['observed_at_utc']);modified=utc(s.get('modified_at_utc') or s.get('published_at_utc') or s['observed_at_utc']);require(published<=modified<=observed<=now,'Source clock invalid')
  prior=old.get(gid)
  if prior:
   require({(p['game_id'],p['player_id'],p['team']) for p in g['players']}=={(p['game_id'],p['player_id'],p['team']) for p in prior['players']},'Original player population replaced')
   require(g['original_forecast']==prior['original_forecast'] and g['original_created_at_utc']==prior['original_created_at_utc'],'Original forecast rewritten')
   require(g['away_team']==prior['away_team'] and g['home_team']==prior['home_team'] and utc(g['kickoff_utc'])==utc(prior['kickoff_utc']),'Fixture rescheduled without review')
   closed=now>=cutoff or prior['state'] in {'LIVE','FINAL','LOCKED','AMBIGUOUS'} or g['state'] in {'LIVE','FINAL','LOCKED','AMBIGUOUS'}
   if closed:require(g['forecast']==prior['forecast'] and g['players']==prior['players'],'Delayed candidate would revise locked public forecasts')
   if g['forecast']!=prior['forecast']:
    require(bool(re.fullmatch('[a-f0-9]{64}',str(g.get('team_input_hash','')))) and g.get('team_input_hash')!=prior.get('team_input_hash') and g.get('numeric_change') is True and 'CONSUMED_TEAM_HISTORY_CHANGED' in g.get('reason',''),'Score change without changed consumed input provenance')
   def player_means(game):return {(p['game_id'],p['player_id'],p['team']):tuple(p[k] for k in ['research_rushing_yards','research_receiving_yards','research_carries','research_targets']) for p in game['players']}
   if player_means(g)!=player_means(prior):require(bool(re.fullmatch('[a-f0-9]{64}',str(g.get('player_input_hash','')))) and g.get('player_input_hash')!=prior.get('player_input_hash') and 'CONSUMED_PLAYER_USAGE_CHANGED' in g.get('reason',''),'Player means changed without compatible consumed input provenance')
   for key,av in prior['availability'].items():
    if av['status'] in ADVERSE:require(g['availability'].get(key,{}).get('status') not in [None,'UNKNOWN'],'Missing status cleared adverse evidence')
  else:require(now<cutoff or all(v is None for v in values),'No first public numerical forecast after cutoff')
 if any(g['game_id'].startswith('2026_08_') for g in data['games']):require(len(seen)==14 and len(ids)==409 and seen=={k for k in old if k.startswith('2026_08_')},'Week 8 protected population differs')
 return True
def verify(site,policy,previous,now,approval=None,require_approval=True):
 site=Path(site);files={p.relative_to(site).as_posix():p.read_bytes() for p in site.rglob('*') if p.is_file()};require(not any(p.is_symlink() for p in site.rglob('*')),'Symlink')
 receipt=json.loads(files['release_receipt.json']);require(receipt['schema']=='hosted-classic-candidate-v1' and receipt['static_parent_release']==PARENT,'UI parent differs')
 require({n:sha(b) for n,b in files.items() if n!='release_receipt.json'}==receipt['files'],'Candidate inventory drift')
 require({n:sha(b) for n,b in files.items() if not dynamic(n)}==policy['static_assets'],'Unapproved design/static change')
 require(all(not n.endswith(('.joblib','.pkl','.pickle','.py','.tar.gz','.csv')) for n in files),'Private asset')
 require(policy['purpose']==PURPOSE and policy['model_hashes']=={'team':TEAM,'player':PLAYER} and not policy['official_replacement'] and not policy['paid_requests'],'Policy expansion')
 if require_approval:
  require(approval and approval.get('decision')=='APPROVED' and approval.get('purpose')==PURPOSE and bool(approval.get('explicit_human_approval_reference')),'Separate unattended approval absent')
  require(approval['policy_sha256']==sha(json.dumps(policy,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()) and utc(approval['approved_at_utc'])<=now<utc(approval['expires_at_utc']),'Approval binding/expiry differs')
 catalog=json.loads(files['data/hosted_catalog.json']);require(catalog['schema']=='hosted-classic-catalog-v1' and catalog['static_parent_release']==PARENT and catalog['model_hashes']==policy['model_hashes'],'Catalog mismatch')
 for week,e in catalog['periods'].items():
  require(8<=int(week)<=18 and e['path']==f'data/research/2026-w{int(week):02d}.json' and sha(files[e['path']])==e['sha256'],'Period hash/scope differs');data=json.loads(files[e['path']]);require(len(data['games'])==e['fixture_count'] and sum(len(g['players']) for g in data['games'])==e['player_count'],'Period population differs');records(data,previous,now)
 require(files['data/weekly_research.json']==files[catalog['periods'][str(catalog['current_week'])]['path']],'Current-period routing differs')
 return {'status':'PASS_RELEASE_DATA_GATE_ONLY','publication_authorized':require_approval,'release_id':receipt['release_id'],'files':len(files)}
