"""One exact Week 11 publication using the reviewed Week 10 security helpers."""
import argparse
import base64
import subprocess
import csv
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import verify_week10_publication as previous

BASE = Path('week11_release')
REPO, SITE, WORKFLOW_ID = previous.REPO, previous.SITE, previous.WORKFLOW_ID
SCOPE = 'WEEK11_REVIEW_RECOVERY_STATIC_ONCE'
PARENT = '67cfd39058ef597a6e79a8dfbbc44d9f6fe03b90'
RECOVERY = {
    'recovery_id': 'week11-workflow-state-review-recovery-v1',
    'supersedes_request_sha256': '82bbafeaae34c52178c932ebb662d4ced2db399938cca8f4dd84076565f9396c',
    'prior_failed_run_id': 44578183871,
}
PRIOR_CREATED = '2026-11-19T16:40:05.851707+00:00'
CONSUMED_COMMENT = 7016684198
FAILED_SUITE = 120669298658
FAILED_JOBS = {'review': 135751297141, 'publish': 135751513177}
WORKFLOW_PATH = '.github/workflows/manual-frontend-correction.yml'
PARENT_MANIFEST = '6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e'
PARENT_RUN = 43425747057
BRANCH = 'codex/week11-review-recovery'
OFFICIAL = 'https://www.nfl.com/schedules/2026/by-week/week-11'
OLD = Path('week10_release')
require, sha, read, clock, api = previous.require, previous.sha, previous.read, previous.clock, previous.api
ET = ZoneInfo('America/New_York')


class ScheduleLinks(HTMLParser):
    """Record only text in exact, fixed NFL game-summary links."""
    def __init__(self):
        super().__init__()
        self.current = None
        self.parts = []
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            path = urlsplit(dict(attrs).get('href', '')).path
            if re.fullmatch(r'/games/[a-z0-9-]+-2026-reg-11', path):
                self.current, self.parts = path, []

    def handle_data(self, content):
        if self.current is not None:
            self.parts.append(content)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            label = ' '.join(' '.join(self.parts).split())
            self.links.setdefault(self.current, set()).add(label)
            self.current = None


def verify_package():
    parent_path = OLD/'publication_manifest.json'
    require(sha(parent_path) == PARENT_MANIFEST, 'The deployed Week 10 parent manifest differs')
    before = read(parent_path)['files']
    require(previous.inventory(OLD/'site') == before, 'The inherited Week 10 assets differ')
    manifest = read(BASE/'publication_manifest.json')
    require(manifest['schema'] == 'week11-reviewed-static-candidate-v1'
            and manifest['repository'] == REPO and manifest['destination'] == SITE
            and manifest['previous_live_manifest_sha256'] == PARENT_MANIFEST
            and manifest['publication_authorized'] is False
            and manifest['production_certified'] is False and manifest['actionable'] is False,
            'Exact research release required')
    after = manifest['files']
    site_id = previous.digest(json.dumps(after, sort_keys=True, separators=(',', ':')).encode())
    require(site_id == manifest['site_identity_sha256']
            and manifest['release_id'] == 'frontend-week11-launch-'+site_id[:16], 'Release identity differs')
    overlay = previous.inventory(BASE/'overlay')
    require(before | overlay == after and list(sorted(overlay)) == manifest['changed_site_files']
            and len(after) == manifest['file_count'] == 440
            and {p:h for p,h in before.items() if p not in overlay} == manifest['frozen_parent_files'],
            'Unexpected, missing or altered site asset')
    require(all(not p.startswith('data/') or p not in before
                or p in {'data/hosted_catalog.json','data/quarter_catalog.json','data/td_catalog.json'}
                for p in overlay), 'Frozen historical data changed')
    return manifest, before


def control_request(manifest, expected_control):
    require(sha(BASE/'controls.json') == expected_control, 'Controls differ from workflow pin')
    controls = read(BASE/'controls.json')
    require(controls['schema'] == 'week11-publication-controls-v1'
            and set(controls['files']) == {
                'scripts/verify_week11_publication.py', 'scripts/verify_week10_publication.py',
                'week11_release/review_request.json', 'tests/test_week11_publication.py',
                'scripts/Invoke-NflPublication.ps1', 'tests/test_nfl_publication_lifecycle.ps1'
            }
            and all(sha(Path(p)) == h for p,h in controls['files'].items()), 'Control file inventory differs')
    path = BASE/'review_request.json'
    d = read(path)
    require(d['schema'] == 'week11-review-recovery-request-v1'
            and all(d.get(k) == v for k,v in RECOVERY.items())
            and d['release_id'] == manifest['release_id']
            and d['manifest_sha256'] == sha(BASE/'publication_manifest.json')
            and d['scope'] == SCOPE and d['repository'] == REPO and d['destination'] == SITE
            and d['publication_deadline_utc'] == manifest['first_cutoff_utc']
            and d['base_main'] == PARENT and d['review_branch'] == BRANCH
            and d['allowed_dispatches'] == 1 and d['unattended'] is False,
            'Review request scope differs')
    return d, sha(path)


def parse_approval(body):
    require(isinstance(body,str) and len(body)<=4096, 'Expected one approval JSON block')
    body = body.replace('\r\n','\n').strip(' \t\n')
    fence = '\x60'*3
    require('\r' not in body and body.startswith(fence+'json\n')
            and body.endswith('\n'+fence) and body.count(fence) == 2, 'Expected one approval JSON block')
    def reject_duplicates(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), 'Duplicate approval field')
        return result
    return json.loads(body[len(fence)+5:-4],object_pairs_hook=reject_duplicates)


def approval(env, request, request_sha, now):
    sha_main = env.get('GITHUB_SHA','')
    require(env.get('GITHUB_REPOSITORY') == REPO and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and env.get('GITHUB_RUN_ATTEMPT') == '1'
            and env.get('GITHUB_ACTOR') == env.get('GITHUB_TRIGGERING_ACTOR') == 'racingmodel'
            and env.get('INSTALLATION_HEAD') == sha_main and re.fullmatch(r'[a-f0-9]{40}',sha_main)
            and all(env.get(k) == 'false' for k in previous.FLAGS)
            and env.get('REQUESTED_RELEASE') == request['release_id'] and env.get('REQUESTED_SHA') == request_sha,
            'Authenticated owner dispatch and reviewed commit required')
    require(api('git/ref/heads/main')['object']['sha'] == sha_main, 'Main changed after review')
    comment_id = env.get('APPROVAL_COMMENT_ID','')
    require(re.fullmatch(r'[1-9][0-9]{0,19}',comment_id),'Owner approval comment ID required')
    require(int(comment_id)!=CONSUMED_COMMENT,'Previous owner approval was consumed')
    item = api('issues/comments/'+comment_id)
    pattern = re.escape(f'https://api.github.com/repos/{REPO}/issues/')+r'([1-9][0-9]*)'
    issue = re.fullmatch(pattern,item.get('issue_url',''))
    require(str(item.get('id')) == comment_id and item.get('user',{}).get('login') == 'racingmodel'
            and item.get('author_association') == 'OWNER' and item['created_at'] == item['updated_at']
            and issue, 'Fresh unedited owner comment on the merged release PR required')
    pr = api('pulls/'+issue[1])
    parents = api('commits/'+sha_main)['parents']
    require(pr.get('merged') is True and pr['merge_commit_sha'] == sha_main
            and pr['base']['ref'] == 'main' and pr['base']['repo']['full_name'] == REPO
            and pr['head']['ref'] == BRANCH and pr['head']['repo']['full_name'] == REPO
            and [p['sha'] for p in parents] == [PARENT,pr['head']['sha']],
            'The owner comment does not identify the reviewed release merge')
    grant = parse_approval(item['body'])
    binding = {k:request[k] for k in ('repository','destination','release_id','manifest_sha256',
                                     'scope','publication_deadline_utc','unattended')}
    binding.update(RECOVERY)
    binding.update(decision='APPROVED',installation_head=sha_main,request_sha256=request_sha)
    require(type(grant) is dict and set(grant) == set(binding)|{'expires_at_utc'}
            and grant['unattended'] is False and all(grant.get(k) == v for k,v in binding.items()),
            'Owner approval scope or identity differs')
    approved, expires = clock(item['created_at']), clock(grant['expires_at_utc'])
    require(max(clock(request['created_at_utc']),clock(pr['merged_at'])) <= approved <= now < expires
            <= min(clock(request['publication_deadline_utc']),approved+timedelta(hours=2)),
            'Approval is stale, expired or extends past the cutoff')
    return approved, expires


def verify_review_failure():
    """Reprove the single prior review failure using authenticated GitHub records."""
    for path, expected in (
        (WORKFLOW_PATH,'73446fdd8d443ff8265fab05539d3ea71bb360c2b9bd068b8972aa9815b29b4b'),
        ('scripts/verify_week11_publication.py','5022bccd15020aca6f28d0655be996483063030208c2c7a7158231d82bf71c59'),
        ('week11_release/review_request.json',RECOVERY['supersedes_request_sha256']),
    ):
        obj=api('contents/'+path,ref=PARENT)
        require(obj.get('encoding')=='base64' and previous.digest(base64.b64decode(obj['content']))==expected,
                'Prior failed attempt control bytes differ')
    verify_failed_records()


def verify_failed_records():
    rid=RECOVERY['prior_failed_run_id']
    run=api(f'actions/runs/{rid}')
    suite=api(f'check-suites/{FAILED_SUITE}')
    require(run.get('id')==rid and run.get('workflow_id')==int(WORKFLOW_ID)
            and run.get('check_suite_id')==suite.get('id')==FAILED_SUITE
            and run.get('head_sha')==suite.get('head_sha')==PARENT
            and suite.get('app',{}).get('slug')=='github-actions'
            and run.get('status')==suite.get('status')=='completed'
            and run.get('conclusion')==suite.get('conclusion')=='failure'
            and run.get('event')=='workflow_dispatch' and run.get('run_attempt')==1
            and run.get('head_branch')=='main' and run.get('path')==WORKFLOW_PATH
            and run.get('repository',{}).get('full_name')==REPO
            and run.get('actor',{}).get('login')==run.get('triggering_actor',{}).get('login')=='racingmodel',
            'Prior run is not the reviewed failed first attempt')
    result=api(f'actions/runs/{rid}/attempts/1/jobs',per_page=100)
    jobs=result.get('jobs',[])
    require(result.get('total_count')==len(jobs)==2 and {j['name'] for j in jobs}==set(FAILED_JOBS),
            'Prior jobs incomplete or ambiguous')
    for job in jobs:
        require(job.get('id')==FAILED_JOBS[job['name']] and job.get('run_id')==rid
                and job.get('run_attempt')==1 and job.get('head_sha')==PARENT and job.get('status')=='completed',
                'Prior job identity differs')
    review=next(j for j in jobs if j['name']=='review')
    publish=next(j for j in jobs if j['name']=='publish')
    require(review.get('conclusion')=='failure' and publish.get('conclusion')=='skipped'
            and publish.get('steps')==[] and publish.get('runner_id') is None,'Prior publish job ran or is ambiguous')
    for name, conclusion in (
        ('Verify new owner approval, frozen assets and fixture clocks','failure'),
        ('Run actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9','skipped'),
    ):
        steps=[s for s in review.get('steps',[]) if s.get('name')==name]
        require(len(steps)==1 and steps[0].get('conclusion')==conclusion,'Prior review failed at a different boundary')
    artifacts=api(f'actions/runs/{rid}/artifacts',per_page=100)
    require(artifacts.get('total_count')==0 and artifacts.get('artifacts')==[],'Prior run has an artifact')
    deployments=api('deployments',per_page=100)
    require(isinstance(deployments,list) and len(deployments)<100
            and all(re.fullmatch(r'[a-f0-9]{40}',str(d.get('sha',''))) for d in deployments)
            and not any(d['sha']==PARENT for d in deployments),'Prior deployment exists or history is ambiguous')


def dispatch_inventory(env, request, approved):
    verify_review_failure()
    rid = env.get('GITHUB_RUN_ID','')
    require(re.fullmatch(r'[1-9][0-9]{0,19}',rid),'Authenticated run required')
    current = api('actions/runs/'+rid)
    keys = ('id','head_sha','event','status','run_attempt','workflow_id')
    require([current.get(k) for k in keys] == [int(rid),env['GITHUB_SHA'],'workflow_dispatch','in_progress',1,int(WORKFLOW_ID)]
            and current['head_branch'] == 'main' and current.get('conclusion') is None
            and current['repository']['full_name'] == REPO and current.get('path') == '.github/workflows/manual-frontend-correction.yml'
            and current['actor']['login'] == current['triggering_actor']['login'] == 'racingmodel'
            and approved <= clock(current['created_at']), 'Direct run does not match this approval and commit')
    repo = previous.all_run_records('actions/runs')
    workflow = previous.all_run_records(f'actions/workflows/{WORKFLOW_ID}/runs')
    a = {r['id']:tuple(r[k] for k in keys) for r in repo if r['workflow_id'] == int(WORKFLOW_ID)}
    b = {r['id']:tuple(r[k] for k in keys) for r in workflow}
    require(a == b and a.get(int(rid)) == tuple(current[k] for k in keys), 'Direct and unfiltered run histories disagree')
    require(any(r['id']==PARENT_RUN and r['conclusion']=='success' for r in workflow),'Successful Week 10 parent run missing')
    require(all(r['status']=='completed' and r['head_sha']!=env['GITHUB_SHA']
                for r in repo if r['id']!=int(rid)), 'Competing or previously attempted dispatch')
    require({r['id'] for r in workflow if clock(r['created_at'])>=clock(PRIOR_CREATED)}
            == {int(rid),RECOVERY['prior_failed_run_id']},'Unreviewed dispatch consumed this recovery request')
    require(clock(current['created_at'])>=clock(request['created_at_utc']),'Run predates the recovery request')
    return {'run_id':int(rid),'repository_records':len(repo),'manual_workflow_records':len(workflow)}


def fixture_check(raw_csv, official_html, manifest, observed):
    paths = {r['game_id']:r for r in read(BASE/'overlay/data/hosted_catalog.json')['periods']['11']['fixtures']}
    period = read(BASE/'overlay/data/weekly_research_week11.json')
    games = {g['game_id']:g for g in period['games']}
    rows = [r for r in csv.DictReader(io.StringIO(raw_csv.decode('utf-8-sig')))
            if (r['season'],r['week'],r['game_type']) == ('2026','11','REG')]
    require(len(rows)==len(paths)==len(games)==13 and {r['game_id'] for r in rows} == set(games) == set(paths)
            == set(manifest['fixture_cutoffs']), 'Fresh Week 11 population differs')
    parser = ScheduleLinks()
    parser.feed(official_html.decode('utf-8'))
    require(set(parser.links) == {urlsplit(r['official_url']).path for r in paths.values()}, 'Official fixture link scope differs')
    for row in rows:
        gid = row['game_id']
        g = games[gid]
        cutoff = clock(manifest['fixture_cutoffs'][gid])
        kickoff = clock(g['kickoff_utc'])
        statuses = {row.get(k,'').strip().upper() for k in ('status','game_status','game_state','state')}
        require((row['away_team'],row['home_team']) == (g['away_team'],g['home_team'])
                and not row['away_score'] and not row['home_score']
                and statuses <= {'','PRE','PRE_KICKOFF','PREGAME','SCHEDULED','NOT_STARTED'}
                and observed < cutoff == kickoff-timedelta(minutes=1)
                and clock(g['original_created_at_utc']) < cutoff, 'Closed, live or ambiguous fixture '+gid)
        labels = parser.links[urlsplit(paths[gid]['official_url']).path]
        stamps = set()
        for label in labels:
            m = re.fullmatch(r'.+? at .+?, \w+, (\w+ \d+)(?:st|nd|rd|th), (\d+:\d+ [AP]M), .+',label)
            if m:
                stamps.add(datetime.strptime('2026 '+m[1]+' '+m[2],'%Y %B %d %I:%M %p').replace(tzinfo=ET))
        require(stamps=={kickoff} and all('FINAL' not in l and 'In Progress' not in l for l in labels),
                'Official schedule disagrees or is ambiguous for '+gid)
        feed = datetime.fromisoformat(row['gameday']+'T'+row['gametime']).replace(tzinfo=ET)
        verified_flex = {'2026_11_PIT_PHI':('2026-11-22T21:25:00+00:00','2026-11-22T18:00:00+00:00'),
                         '2026_11_JAX_NYG':('2026-11-22T18:00:00+00:00','2026-11-22T21:25:00+00:00')}
        require(feed==kickoff or verified_flex.get(gid)==(feed.astimezone(timezone.utc).isoformat(),kickoff.isoformat()),
                'Independent schedule differs for '+gid)
    return {'fixtures':len(rows),'observed_at_utc':observed.isoformat(),'schedule_sha256':previous.digest(raw_csv),
            'official_schedule_sha256':previous.digest(official_html)}


def operator_preflight(comment_id, workflow_enabled):
    """Read-only checked authorisation for the local operator, using the saved gh login."""
    def cli_get(path, **params):
        from urllib.parse import urlencode
        location=f'repos/{REPO}/{path}' + ('?'+urlencode(params) if params else '')
        return gh_get(location)

    def gh_get(path):
        reply=subprocess.run(['gh','api','--method','GET',path],capture_output=True,check=True,timeout=20)
        return json.loads(reply.stdout)

    require(gh_get('user').get('login')=='racingmodel','Use the authenticated repository owner')
    global api
    api=previous.api=cli_get
    manifest,_=verify_package()
    workflow=Path(WORKFLOW_PATH)
    pin=re.search(r'CONTROL_SHA256: "([a-f0-9]{64})"',workflow.read_text(encoding='utf-8'))[1]
    request,request_sha=control_request(manifest,pin)
    head=api('git/ref/heads/main')['object']['sha']
    for path in (WORKFLOW_PATH,'week11_release/controls.json','week11_release/publication_manifest.json'):
        remote=api('contents/'+path,ref=head)
        require(remote.get('encoding')=='base64' and previous.digest(base64.b64decode(remote['content']))==sha(Path(path)),
                'Installed workflow or release differs from local package')
    env=dict(GITHUB_REPOSITORY=REPO,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',
             GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='racingmodel',GITHUB_TRIGGERING_ACTOR='racingmodel',
             INSTALLATION_HEAD=head,GITHUB_SHA=head,REQUESTED_RELEASE=manifest['release_id'],REQUESTED_SHA=request_sha,
             APPROVAL_COMMENT_ID=comment_id)
    variables=api('actions/variables',per_page=100)['variables']
    env.update({row['name']:row['value'] for row in variables if row['name'] in previous.FLAGS})
    _,expires=approval(env,request,request_sha,previous.now())
    if workflow_enabled:
        previous.verify_dispatch_workflow_states()
    else:
        states=api('actions/workflows',per_page=100)
        require(states.get('total_count')==len(states['workflows'])==5
                and sum(str(w['id'])==WORKFLOW_ID for w in states['workflows'])==1
                and all(w['state']=='disabled_manually' for w in states['workflows']),'All workflows must be disabled initially')
    require(api('pages').get('build_type')=='workflow','Wrong Pages mode')
    verify_review_failure()
    deployments=api('deployments',environment='github-pages',per_page=100)
    require(len(deployments)<100,'Deployment history incomplete')
    previous.verify_deployments(deployments,env,'review')
    repo=previous.all_run_records('actions/runs')
    workflow_runs=previous.all_run_records(f'actions/workflows/{WORKFLOW_ID}/runs')
    require({r['id'] for r in repo if str(r['workflow_id'])==WORKFLOW_ID}=={r['id'] for r in workflow_runs}
            and all(r['status']=='completed' and r['head_sha']!=head for r in repo)
            and {r['id'] for r in workflow_runs if clock(r['created_at'])>=clock(PRIOR_CREATED)}=={RECOVERY['prior_failed_run_id']},
            'Active or previously attempted publication')
    schedule,official=previous.download(previous.SCHEDULE),previous.download(OFFICIAL)
    fixture_check(schedule,official,manifest,previous.now())
    require(previous.now()<expires,'Approval expired during local check')
    return dict(head=head,release_id=manifest['release_id'],request_sha=request_sha,
                comment_id=comment_id,expires_at_utc=expires.isoformat(),deadline_utc=manifest['first_cutoff_utc'],
                baseline_ids=sorted(r['id'] for r in workflow_runs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase',choices=['review','pre-deploy','live','operator-preflight'])
    parser.add_argument('--out',type=Path)
    parser.add_argument('--approval-id')
    parser.add_argument('--workflow-enabled',action='store_true')
    parser.add_argument('--stage',type=Path)
    args = parser.parse_args()
    if args.phase=='operator-preflight':
        print(json.dumps(operator_preflight(args.approval_id or '',args.workflow_enabled)))
        return
    require(args.out is not None,'Receipt output is required')
    manifest, parent = verify_package()
    env = os.environ
    pin=env.get('CONTROL_SHA256')
    if args.phase=='live' and not pin:
        pin=re.search(r'CONTROL_SHA256: "([a-f0-9]{64})"',Path(WORKFLOW_PATH).read_text(encoding='utf-8'))[1]
    request, request_sha = control_request(manifest,pin)
    result = {'release_id':manifest['release_id'],'phase':args.phase,'status':'PASS',
              'manifest_sha256':sha(BASE/'publication_manifest.json'),'request_sha256':request_sha}
    if args.phase == 'live':
        served = previous.live_check(manifest['files'],5,env['GITHUB_RUN_ID'])
        require(all(r['match'] for r in served),'Week 11 served verification failed')
        result['served'] = served
    else:
        approved, expires = approval(env,request,request_sha,previous.now())
        previous.verify_dispatch_workflow_states()
        require(api('pages').get('build_type')=='workflow','Wrong Pages mode')
        deployments = api('deployments',environment='github-pages',per_page=100)
        require(len(deployments)<100,'Deployment history incomplete')
        result['deployments'] = previous.verify_deployments(deployments,env,args.phase)
        result['dispatch'] = dispatch_inventory(env,request,approved)
        verify_paths = parent if args.phase=='review' else {p:parent[p] for p in
                       ('index.html','classic_research.js','week10_preview.js','data/hosted_catalog.json')}
        require(all(r['match'] for r in previous.live_check(verify_paths,2,env['GITHUB_RUN_ID']+'-'+args.phase)),
                'Live parent hashes differ')
        schedule = previous.download(previous.SCHEDULE)
        official = previous.download(OFFICIAL)
        result['fixtures'] = fixture_check(schedule,official,manifest,previous.now())
        require(previous.now()<expires,'Approval expired during verification')
        if args.phase=='review':
            require(args.stage is not None and not args.stage.exists(),'A fresh staging path is required')
            shutil.copytree(OLD/'site',args.stage)
            shutil.copytree(BASE/'overlay',args.stage,dirs_exist_ok=True)
            require(previous.inventory(args.stage)==manifest['files'],'Staged Pages bytes differ')
        require(api('git/ref/heads/main')['object']['sha']==env['GITHUB_SHA'] and previous.now()<expires,
                'Main or approval changed during preflight')
    with args.out.open('x',encoding='utf-8') as handle:
        json.dump(result,handle,indent=2,ensure_ascii=True)
        handle.write('\n')


if __name__=='__main__':
    main()
