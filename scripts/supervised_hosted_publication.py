"""Exact one-time hosted-to-Pages gate. No inference, secrets or schedule."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import argparse, csv, hashlib, io, json, os, time, urllib.request
from supervised_release_data_gate import verify as data_verify

ROOT=Path(__file__).resolve().parents[1]
SITE='https://racingmodel.github.io/nfl-2026-visualizer/'
SCHEDULE='https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv'
PURPOSE='ONE_SUPERVISED_HOSTED_WEEK8_PUBLICATION'

def load(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc(s): return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(timezone.utc)
def now(): return datetime.now(timezone.utc)
def require(ok,msg):
    if not ok: raise ValueError(msg)
def save(p,d): Path(p).write_text(json.dumps(d,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')
def request():
    r=load(ROOT/'hosted_handoff/request.json')
    require(r['purpose']==PURPOSE and r['decision']=='APPROVED_ONE_TIME' and r['unattended_authorized'] is False,'One-time scope missing')
    return r

def package(site):
    r=request();site=Path(site);receipt=load(site/'release_receipt.json')
    require(sha(site/'release_receipt.json')==r['candidate_receipt_sha256'] and receipt['release_id']==r['candidate_release_id'],'Exact reviewed export differs')
    previous=load(ROOT/'correction_release/site/data/weekly_research.json')
    data_verify(site,load(ROOT/'hosted_handoff/data_policy.json'),[previous],now(),require_approval=False)
    data=load(site/'data/weekly_research.json');old={g['game_id']:g for g in previous['games']}
    require(len(data['games'])==14 and sum(len(g['players']) for g in data['games'])==409,'Week 8 census differs')
    for g in data['games']:
        p=old[g['game_id']]
        require(g['forecast']==p['forecast'] and g['original_forecast']==p['original_forecast'] and g['players']==p['players'],'One-time numerical/player scope expanded')
        require(g['original_created_at_utc']==p['original_created_at_utc'],'Original issue time changed')
    return r,data,receipt

def live(files,tag,attempts=1):
    def check(item):
        n,h=item
        try:
            with urllib.request.urlopen(SITE+n+'?hosted='+tag,timeout=20) as f:actual=hashlib.sha256(f.read()).hexdigest()
            return dict(path=n,expected=h,actual=actual,match=actual==h)
        except Exception as exc:return dict(path=n,expected=h,match=False,error=type(exc).__name__)
    for attempt in range(attempts):
        with ThreadPoolExecutor(max_workers=8) as pool:checks=list(pool.map(check,files.items()))
        if all(c['match'] for c in checks):return dict(status='PASS_ALL_LIVE_ASSETS',actual_at_utc=now().isoformat(),files=len(checks),checks=checks)
        if attempt+1<attempts:time.sleep(5)
    raise ValueError('Live propagation mismatch: '+','.join(c['path'] for c in checks if not c['match'])[:500])

def fixture_state(data,out):
    with urllib.request.urlopen(SCHEDULE,timeout=20) as f:raw=f.read()
    observed=now();Path(out).mkdir(exist_ok=True);Path(out,'independent_schedule.csv').write_bytes(raw)
    rows=[r for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))) if r['season']=='2026' and r['week']=='8' and r['game_type']=='REG']
    indexed={r['game_id']:r for r in rows};require(len(indexed)==14,'Fresh Week 8 fixture population differs')
    states=[]
    for g in data['games']:
        row=indexed[g['game_id']]
        require(row['home_team']==g['home_team'] and row['away_team']==g['away_team'],'Fresh exact fixture identity differs')
        kickoff=datetime.fromisoformat(row['gameday']+'T'+row['gametime']).replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
        require(kickoff==utc(g['kickoff_utc']) and kickoff-timedelta(minutes=1)==utc(g['cutoff_utc']),'Fresh kickoff/cutoff differs')
        require(not row.get('home_score') and not row.get('away_score') and observed<utc(g['cutoff_utc']),'Fixture started, final or cutoff passed; no supervised replacement')
        states.append(dict(game_id=g['game_id'],state='PRE_KICKOFF',kickoff_utc=kickoff.isoformat(),first_snap_claimed=False))
    d=dict(status='PASS_FRESH_INDEPENDENT_FIXTURES',observed_at_utc=observed.isoformat(),url=SCHEDULE,raw_sha256=hashlib.sha256(raw).hexdigest(),fixtures=states)
    save(Path(out,'fixture_gate.json'),d);return d

def authority(r):
    env=os.environ
    require(env.get('GITHUB_ACTOR')==env.get('GITHUB_TRIGGERING_ACTOR')=='racingmodel','Authenticated owner manual execution required')
    require(env.get('GITHUB_REPOSITORY')=='racingmodel/nfl-2026-visualizer' and env.get('GITHUB_REF')=='refs/heads/main' and env.get('GITHUB_EVENT_NAME')=='workflow_dispatch','Default-branch manual scope required')
    require(env.get('GITHUB_RUN_ATTEMPT')=='1','Rerun not authorized; new review required')
    require(env.get('REQUESTED_HEAD')==env.get('GITHUB_SHA'),'Reviewed installation commit differs')
    require(env.get('REQUEST_SHA')==sha(ROOT/'hosted_handoff/request.json'),'One-time request binding differs')
    require(utc(r['not_before_utc'])<=now()<utc(r['expires_at_utc']),'One-time request expired or future')
    require(all(env.get(k)=='false' for k in ['RESEARCH_AUTOMATION_APPROVED','RESEARCH_PUBLISH_ENABLED','INITIAL_RESEARCH_RELEASE_ENABLED']),'Other publication authority enabled')
    for n,h in r['implementation_files'].items():require(sha(ROOT/n)==h,'Reviewed public integration drift: '+n)
    # A successful or interrupted deployment attempt consumes this grant. Failed preflight tests do not.
    url='https://api.github.com/repos/racingmodel/nfl-2026-visualizer/actions/workflows/supervised-hosted-handoff.yml/runs?per_page=100'
    headers={'Authorization':'Bearer '+env['GH_TOKEN'],'Accept':'application/vnd.github+json'}
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=20) as f:runs=json.loads(f.read())['workflow_runs']
    for run in runs:
        if str(run['id'])==env['GITHUB_RUN_ID']:continue
        if utc(run['created_at'])<utc(r['not_before_utc']):continue
        with urllib.request.urlopen(urllib.request.Request(run['jobs_url'],headers=headers),timeout=20) as f:jobs=json.loads(f.read())['jobs']
        require(not any(s['name']=='Deploy reviewed hosted site' and s.get('conclusion') not in [None,'skipped'] for j in jobs for s in j.get('steps',[])),'One-time publication already attempted; grant consumed')

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['verify','live','rollback-live','offline']);p.add_argument('--site',default='candidate-site');p.add_argument('--mode',choices=['publish','failure_test'],default='publish');a=p.parse_args();out=Path('handoff-evidence');out.mkdir(exist_ok=True)
    if a.command=='rollback-live':
        m=load(ROOT/'correction_release/deployment_manifest.json');save(out/'rollback_live.json',live(m['files'],'rollback-'+os.environ.get('GITHUB_RUN_ID','local'),attempts=8));return
    r,data,receipt=package(a.site)
    if a.command=='offline':print(json.dumps(dict(status='PASS_EXACT_REVIEWED_EXPORT',release=receipt['release_id'],publication=False)));return
    if a.command=='live':
        files=dict(receipt['files']);files['release_receipt.json']=sha(Path(a.site)/'release_receipt.json');save(out/'live_assets.json',live(files,r['candidate_receipt_sha256'][:16],attempts=8));return
    authority(r);before=live(load(ROOT/'correction_release/deployment_manifest.json')['files'],'before-'+os.environ['GITHUB_RUN_ID']);save(out/'live_before.json',before)
    fixture_state(data,out)
    if a.mode=='failure_test':
        path=Path(a.site)/'index.html';path.write_bytes(path.read_bytes()+b'\n<!-- isolated failure test only -->')
        rejected=False
        try:package(a.site)
        except ValueError:rejected=True
        require(rejected,'Corrupt artifact was not rejected')
        after=live(load(ROOT/'correction_release/deployment_manifest.json')['files'],'failed-'+os.environ['GITHUB_RUN_ID']);save(out/'expected_failure.json',dict(status='PASS_EXPECTED_DEPLOYMENT_PREFLIGHT_FAILURE_LKG_UNCHANGED',candidate_rejected=True,deploy_permission_used=False,live_files_unchanged=after['files'],actual_at_utc=now().isoformat()))
        raise SystemExit(19)
    save(out/'publication_gate.json',dict(status='PASS_ONE_SUPERVISED_HOSTED_PUBLICATION',purpose=PURPOSE,actual_at_utc=now().isoformat(),candidate_release=receipt['release_id'],candidate_receipt_sha256=r['candidate_receipt_sha256'],package=r['package'],github_run_id=os.environ['GITHUB_RUN_ID'],github_commit=os.environ['GITHUB_SHA'],fixtures=14,players=409,unattended_authorized=False))
if __name__=='__main__':main()
