"""One exact Week 11 publication using the reviewed Week 10 security helpers."""
import argparse
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
SCOPE = 'WEEK11_PREGAME_STATIC_ONCE'
PARENT = '9bb938291247ea64f004886fdff90f57bc86c753'
PARENT_MANIFEST = '6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e'
PARENT_RUN = 43425747057
BRANCH = 'codex/week11-launch-2026'
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
                'week11_release/review_request.json', 'tests/test_week11_publication.py'
            }
            and all(sha(Path(p)) == h for p,h in controls['files'].items()), 'Control file inventory differs')
    path = BASE/'review_request.json'
    d = read(path)
    require(d['schema'] == 'week11-owner-review-request-v1'
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
    binding.update(decision='APPROVED',installation_head=sha_main,request_sha256=request_sha)
    require(type(grant) is dict and set(grant) == set(binding)|{'expires_at_utc'}
            and grant['unattended'] is False and all(grant.get(k) == v for k,v in binding.items()),
            'Owner approval scope or identity differs')
    approved, expires = clock(item['created_at']), clock(grant['expires_at_utc'])
    require(max(clock(request['created_at_utc']),clock(pr['merged_at'])) <= approved <= now < expires
            <= min(clock(request['publication_deadline_utc']),approved+timedelta(hours=2)),
            'Approval is stale, expired or extends past the cutoff')
    return approved, expires


def dispatch_inventory(env, request, approved):
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
    require({r['id'] for r in workflow if clock(r['created_at']) >= clock(request['created_at_utc'])} == {int(rid)},
            'Earlier dispatch consumed this Week 11 request')
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase',choices=['review','pre-deploy','live'])
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--stage',type=Path)
    args = parser.parse_args()
    manifest, parent = verify_package()
    env = os.environ
    request, request_sha = control_request(manifest,env.get('CONTROL_SHA256'))
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
