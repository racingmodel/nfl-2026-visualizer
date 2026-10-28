"""Exact owner-approved manual correction gate; no new remote secrets/settings."""
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import argparse,hashlib,json,os,sys,urllib.request
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.weekly_publication import verify_snapshot
PURPOSE='ONE_TIME_EXACT_FRONTEND_CORRECTION'
RELEASE='frontend-classic-week8-974591f0e1ca648d'
MANIFEST='50c40e4435b10a65ad375f0e3ef2402dc685dbcbd24988c60709282493dc5e9d'
DATA='b196e9dad4f55f716e222aa939d2d88445eab7b84149f776e76ea2a7f99afdc3'
SITE='https://racingmodel.github.io/nfl-2026-visualizer/'
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def utc(s):
 d=datetime.fromisoformat(s.replace('Z','+00:00'))
 if d.tzinfo is None:raise ValueError('Explicit UTC required')
 return d.astimezone(timezone.utc)
def require(ok,msg):
 if not ok:raise ValueError(msg)
def package(root):
 r=root/'correction_release';m=load(r/'deployment_manifest.json');p=load(r/'publication_policy.json');q=load(r/'review_request.json')
 require(sha(r/'deployment_manifest.json')==MANIFEST==q['deployment_manifest_sha256'],'Approved manifest differs')
 require(q['release_id']==m['candidate_release_id']==RELEASE and q['purpose']==PURPOSE,'Approved release/scope differs')
 require(sha(r/'publication_policy.json')==q['policy_sha256'],'Reviewed policy differs')
 require(q['unattended_authorized'] is False and p['unattended_authorized'] is False and q['initial_publication_grant_reusable'] is False,'Scope expanded')
 require(all(p[k] is False for k in ['model_changes','numerical_changes','paid_requests','official_replacement']),'Prohibited change')
 data=verify_snapshot(r/'site',m,p)
 require(len(m['files'])==370 and sha(r/'site/data/weekly_research.json')==DATA,'Approved source/assets differ')
 require(all(g['actionable'] is False and g['model_certification']=='NOT_PRODUCTION_CERTIFIED' for g in data['games']),'Research label/actionability differs')
 require(all(x['actionable'] is False and x['verified_active'] is False and all(x[k] is None for k in ['anytime_td_probability','passing_yards','quarter_scores']) for g in data['games'] for x in g['players']),'Player scope differs')
 return r,m,p,q
def verify(root=ROOT,env=None,now=None):
 env=os.environ if env is None else env;now=datetime.now(timezone.utc) if now is None else now
 r,m,p,q=package(root)
 require(env.get('GITHUB_ACTOR')==env.get('GITHUB_TRIGGERING_ACTOR')=='racingmodel','Authenticated owner manual execution required')
 require(env.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and env.get('GITHUB_REF')=='refs/heads/main' and env.get('GITHUB_REPOSITORY')=='racingmodel/nfl-2026-visualizer','Manual default-branch repository scope required')
 require(all(env.get(k)=='false' for k in ['RESEARCH_AUTOMATION_APPROVED','RESEARCH_PUBLISH_ENABLED','INITIAL_RESEARCH_RELEASE_ENABLED']),'Other publication scope enabled')
 require(env.get('REQUESTED_RELEASE')==RELEASE and env.get('REQUESTED_SHA')=='9eee117d77b66fc4da9317aea92b9622ad41bc1a9ba1a4f75deb7f64081a2fc0'==sha(r/'review_request.json'),'Exact reviewed request differs')
 c=root/'config/frontend_correction_controls.json';control_sha=sha(c)
 require(env.get('CORRECTION_CONTROL_SHA256')==control_sha,'Workflow-pinned implementation differs')
 for n,h in load(c)['files'].items():
  path=(root/n).resolve();require(path.is_relative_to(root.resolve()) and path.is_file() and sha(path)==h,'Deployment implementation differs: '+n)
 require(sha(r/'approval.json')==env.get('CORRECTION_APPROVAL_SHA256'),'Workflow-pinned human approval differs')
 a=load(r/'approval.json');g=a['grant']
 require(a.get('decision')=='APPROVED' and a.get('authorization_method')=='EXPLICIT_USER_APPROVAL_AUTHENTICATED_OWNER_MANUAL_DISPATCH','Fresh explicit manual approval absent')
 require(g['purpose']==PURPOSE and g['release_id']==RELEASE and g['deployment_manifest_sha256']==MANIFEST and g['request_sha256']==sha(r/'review_request.json') and g['policy_sha256']==sha(r/'publication_policy.json'),'Grant binding differs')
 require(g['unattended_authorized'] is False and bool(g['explicit_human_approval_reference']),'Explicit one-time scope absent')
 require(utc(q['created_at_utc'])<=utc(g['approved_at_utc'])<=now<utc(g['expires_at_utc'])<=utc(q['review_expires_at_utc']),'Approval/review expired or future')
 return dict(status='PASS_EXACT_ONE_TIME_FRONTEND_CORRECTION',actual_at_utc=now.isoformat(),release_id=RELEASE,deployment_manifest_sha256=MANIFEST,candidate_manifest_sha256=m['candidate_manifest_sha256'],control_sha256=control_sha,files=370,unattended_authorized=False)
def live(root=ROOT):
 r,m,p,q=package(root)
 def check(item):
  n,h=item
  try:
   with urllib.request.urlopen(SITE+n+'?correction='+MANIFEST[:16],timeout=25) as response:actual=hashlib.sha256(response.read()).hexdigest()
   return dict(path=n,expected=h,actual=actual,match=actual==h)
  except Exception as exc:return dict(path=n,expected=h,match=False,error=type(exc).__name__)
 with ThreadPoolExecutor(max_workers=8) as pool:checks=list(pool.map(check,m['files'].items()))
 return dict(status='PASS_LIVE_EXACT_FRONTEND_CORRECTION' if all(x['match'] for x in checks) else 'FAIL_LIVE_ASSET_PROPAGATION',actual_at_utc=datetime.now(timezone.utc).isoformat(),release_id=RELEASE,deployment_manifest_sha256=MANIFEST,checks=checks,github_run_id=os.environ.get('GITHUB_RUN_ID'),unattended_authorized=False)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['verify','live']);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 if a.out.exists():raise ValueError('New immutable receipt required')
 d=verify() if a.command=='verify' else live();a.out.write_text(json.dumps(d,indent=2,ensure_ascii=True)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in d.items() if k!='checks'}))
 if d['status'].startswith('FAIL'):raise SystemExit(1)
