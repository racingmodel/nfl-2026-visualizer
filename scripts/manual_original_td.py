"""One exact owner-approved manual research release. No fitting or signing secrets."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from concurrent.futures import ThreadPoolExecutor
import argparse,hashlib,json,os,sys,urllib.request,time,csv,io
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.weekly_publication import verify_snapshot,inventory
PURPOSE='ONE_TIME_EXACT_FRONTEND_CORRECTION'
RELEASE='frontend-original-td-cb659b1cfe683de3'
MANIFEST='4a2ef1853d79cc6419f4a76b1317f154be81675a8fdd3d5fda26263501b73922'
DATA='25cf91d77a70c6ae05a94230e0245d97f11a886d62a98c158bc22446b5866ad8'
SITE='https://racingmodel.github.io/nfl-2026-visualizer/'
BASE='original_td_release'
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc(s):
 d=datetime.fromisoformat(s.replace('Z','+00:00'))
 if d.tzinfo is None:raise ValueError('Explicit UTC required')
 return d.astimezone(timezone.utc)
def clock():return datetime.now(timezone.utc)
def require(ok,msg):
 if not ok:raise ValueError(msg)
def package(root=ROOT):
 r=Path(root)/BASE;m=load(r/'deployment_manifest.json');p=load(r/'publication_policy.json');q=load(r/'review_request.json')
 require(sha(r/'deployment_manifest.json')==MANIFEST==q['deployment_manifest_sha256'],'Approved manifest differs')
 require(q['release_id']==m['candidate_release_id']==RELEASE and q['purpose']==PURPOSE,'Release scope differs')
 require(sha(r/'publication_policy.json')==q['policy_sha256'],'Reviewed policy differs')
 require(q['unattended_authorized'] is False and p['unattended_authorized'] is False and q['initial_publication_grant_reusable'] is False,'Scope expanded')
 require(all(p[k] is False for k in ['model_changes','raw_numerical_changes','paid_requests','official_replacement']),'Prohibited change')
 data=verify_snapshot(r/'site',m,p)
 require(len(m['files'])==376 and sha(r/'site/data/research/2026-w08.json')==DATA,'Exact approved site/source differs')
 old=load(r/'last_known_good/site/data/research/2026-w08.json')
 require(data==old,'Original forecasts/players/context/lineage changed')
 require(all(g['actionable'] is False and g['model_certification']=='NOT_PRODUCTION_CERTIFIED' for g in data['games']),'Research designation differs')
 require(all(x['actionable'] is False and x['verified_active'] is False and all(x[k] is None for k in ['anytime_td_probability','passing_yards','quarter_scores']) for g in data['games'] for x in g['players']),'Original player scope differs')
 ext=load(r/'site/data/prediction_display.json');require(len(ext['scorelines'])==14 and len(ext['passing'])==27 and ext['calibrated_exact_score_probabilities'] is False,'Approved extension population differs')
 require(ext['component_limitations']['TD']['numeric_rows']==0 and ext['component_limitations']['quarters']['numeric_fixtures']==0,'Uncertified values added')
 td=load(r/'site/data/original_td_component.json')
 require(td['model_sha256']=='530313ab8d23d642906943342d88541e27369515f1e0a87c35ad706b94d8011f' and td['parent_period_sha256']==DATA and td['actionable'] is False,'TD model/parent scope differs')
 require(len(td['rows'])==409 and len(td['teams'])==26 and len({x['game_id'] for x in td['rows']})==13,'TD population differs')
 keys={(g['game_id'],x['team'],x['player_id']) for g in data['games'] for x in g['players']}
 require(len(keys)==409 and keys=={(x['game_id'],x['team'],x['player_id']) for x in td['rows']},'TD identities differ')
 for x in td['rows']:
  require(x['actionable'] is False and x['gameday_active_confirmed'] is False and 0<=x['experimental_td_probability']<=1 and x['model_sha256']==td['model_sha256'] and utc(x['created_at_utc'])<utc(x['cutoff_utc']),'TD output contract differs')
 for t in td['teams']:
  game=next(g for g in data['games'] if g['game_id']==t['game_id']);points=game['forecast']['away_expected_points' if t['team']==game['away_team'] else 'home_expected_points'];target=min(4.25,max(.35,points/7*.72));pool=[x for x in td['rows'] if x['game_id']==t['game_id'] and x['team']==t['team']]
  require(t['parent_expected_final_points']==points and t['budget']==target and len(pool)==t['population'] and abs(sum(x['experimental_td_probability'] for x in pool)-target)<1e-8,'TD expected-point budget differs')
 require(not any(x['game_id']=='2026_08_BAL_BUF' for x in td['rows']),'BAL-BUF must remain withheld')
 require(p['new_conditional_TD_component'] is True and p['TD_calibration_claim'] is False and p['TD_betting_or_fair_odds'] is False,'TD policy expansion')

 for g in data['games']:
  d=next(x for x in ext['scorelines'] if x['game_id']==g['game_id'])
  require(d['raw_forecast']==g['forecast'] and d['source_model_sha256']==g['model_sha256'] and d['actionable'] is False,'Score parent/identity differs')
  require(type(d['snapped_home']) is int and type(d['snapped_away']) is int,'Display not integer')
  if g['game_id'] in ['2026_08_LV_NYJ','2026_08_MIN_DET']:require((d['snapped_away'],d['snapped_home'],d['toss_up'])==(22,23,True),'Approved Toss-up differs')
 require(all(x['actionable'] is False and x['starter_confirmed'] is False and utc(x['created_at_utc'])<utc(x['cutoff_utc']) for x in ext['passing']),'Conditional passing scope differs')
 lkg=load(r/'last_known_good/manifest.json');require(inventory(r/'last_known_good/site')==lkg['files'] and lkg['release_id']=='frontend-prediction-first-6b92212669a57916','Last-known-good differs')
 return r,m,p,q
def verify(root=ROOT,env=None,now=None):
 env=os.environ if env is None else env;now=clock() if now is None else now;r,m,p,q=package(root)
 require(env.get('GITHUB_ACTOR')==env.get('GITHUB_TRIGGERING_ACTOR')=='racingmodel','Authenticated owner-only dispatch required')
 require(env.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and env.get('GITHUB_REF')=='refs/heads/main' and env.get('GITHUB_REPOSITORY')=='racingmodel/nfl-2026-visualizer','Manual default-branch scope required')
 require(env.get('GITHUB_RUN_ATTEMPT')=='1','Rerun not authorized')
 require(all(env.get(k)=='false' for k in ['RESEARCH_AUTOMATION_APPROVED','RESEARCH_PUBLISH_ENABLED','INITIAL_RESEARCH_RELEASE_ENABLED']),'Other publication authority enabled')
 require(env.get('REQUESTED_RELEASE')==RELEASE and env.get('REQUESTED_SHA')==sha(r/'review_request.json'),'Exact reviewed request differs')
 require(env.get('REQUESTED_HEAD')==env.get('GITHUB_SHA'),'Reviewed installation commit differs')
 c=Path(root)/'config/original_td_controls.json';require(sha(c)==env.get('CORRECTION_CONTROL_SHA256'),'Pinned controls differ')
 for n,h in load(c)['files'].items():
  path=(Path(root)/n).resolve();require(path.is_relative_to(Path(root).resolve()) and path.is_file() and sha(path)==h,'Implementation/approval differs: '+n)
 require(sha(r/'approval.json')==env.get('CORRECTION_APPROVAL_SHA256'),'Pinned explicit approval differs')
 a=load(r/'approval.json');g=a['grant']
 require(a.get('decision')=='APPROVED' and a.get('authorization_method')=='EXPLICIT_USER_APPROVAL_AUTHENTICATED_OWNER_MANUAL_DISPATCH','Fresh human authority absent')
 require(g['purpose']==PURPOSE and g['release_id']==RELEASE and g['deployment_manifest_sha256']==MANIFEST and g['request_sha256']==sha(r/'review_request.json') and g['policy_sha256']==sha(r/'publication_policy.json'),'Grant binding differs')
 require(g['unattended_authorized'] is False and bool(g['explicit_human_approval_reference']),'One-time scope absent')
 require(utc(q['created_at_utc'])<=utc(g['approved_at_utc'])<=now<utc(g['expires_at_utc'])<=utc(q['review_expires_at_utc']),'Approval expired/future')
 return dict(status='PASS_EXACT_ONE_TIME_PREDICTION_FIRST_PUBLICATION',actual_at_utc=now.isoformat(),release_id=RELEASE,deployment_manifest_sha256=MANIFEST,files=376,unattended_authorized=False)
def api(url):
 request=urllib.request.Request(url,headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'})
 with urllib.request.urlopen(request,timeout=30) as f:return json.loads(f.read())
def require_unused(root=ROOT):
 r=Path(root)/BASE;grant=load(r/'approval.json')['grant'];after=utc(grant['approved_at_utc'])
 runs=api('https://api.github.com/repos/racingmodel/nfl-2026-visualizer/actions/workflows/manual-frontend-correction.yml/runs?per_page=100')['workflow_runs']
 for run in runs:
  if str(run['id'])==os.environ['GITHUB_RUN_ID'] or utc(run['created_at'])<after:continue
  jobs=api(run['jobs_url'])['jobs']
  require(not any(s['name']=='Deploy exact manually approved artifact' and s.get('conclusion') not in [None,'skipped'] for j in jobs for s in j.get('steps',[])),'One-time deployment authority consumed')
def fixture_gate(data):
 url='https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv'
 with urllib.request.urlopen(url,timeout=30) as f:raw=f.read()
 observed=clock();rows={r['game_id']:r for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))) if r['season']=='2026' and r['week']=='8' and r['game_type']=='REG'}
 require(len(rows)==14,'Fresh fixture population differs')
 states=[]
 for g in data['games']:
  r=rows[g['game_id']];kickoff=datetime.fromisoformat(r['gameday']+'T'+r['gametime']).replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
  require((r['away_team'],r['home_team'])==(g['away_team'],g['home_team']) and kickoff==utc(g['kickoff_utc']) and kickoff-timedelta(minutes=1)==utc(g['cutoff_utc']),'Fresh fixture identity/cutoff differs')
  require(not r['home_score'] and not r['away_score'] and observed<utc(g['cutoff_utc']),'Fixture closed or scores ambiguous')
  states.append(dict(game_id=g['game_id'],state='PRE_KICKOFF',first_snap_claimed=False))
 Path('original_td_schedule.csv').write_bytes(raw)
 return dict(observed_at_utc=observed.isoformat(),source_url=url,sha256=hashlib.sha256(raw).hexdigest(),fixtures=states)
def live_files(files,attempts=8):
 def check(item):
  n,h=item
  try:
   with urllib.request.urlopen(SITE+n+'?prediction-first='+MANIFEST[:16],timeout=25) as response:actual=hashlib.sha256(response.read()).hexdigest()
   return dict(path=n,expected=h,actual=actual,match=actual==h)
  except Exception as exc:return dict(path=n,expected=h,match=False,error=type(exc).__name__)
 for attempt in range(attempts):
  with ThreadPoolExecutor(max_workers=8) as pool:checks=list(pool.map(check,files.items()))
  if all(x['match'] for x in checks):return dict(status='PASS_ALL_LIVE_ASSETS',actual_at_utc=clock().isoformat(),files=len(files),checks=checks)
  if attempt+1<attempts:time.sleep(5)
 return dict(status='FAIL_LIVE_PROPAGATION',actual_at_utc=clock().isoformat(),files=len(files),checks=checks)
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['verify','live','rollback-live']);p.add_argument('--out',type=Path,required=True);a=p.parse_args();require(not a.out.exists(),'Receipt already exists')
 if a.command=='verify':
  result=verify();require_unused();r,m,policy,q=package();lkg=load(r/'last_known_good/manifest.json');before=live_files(lkg['files'],attempts=2);require(before['status']=='PASS_ALL_LIVE_ASSETS','Live parent drift; stop before deployment');result['last_known_good_live_files']=before['files'];result['fixture_gate']=fixture_gate(load(r/'site/data/weekly_research.json'))
 elif a.command=='live':
  r,m,policy,q=package();result=live_files(m['files']);result.update(release_id=RELEASE,github_run_id=os.environ.get('GITHUB_RUN_ID'))
 else:
  r,m,policy,q=package();result=live_files(load(r/'last_known_good/manifest.json')['files'])
 a.out.write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='checks'}))
 if result['status'].startswith('FAIL'):raise SystemExit(1)
if __name__=='__main__':main()
