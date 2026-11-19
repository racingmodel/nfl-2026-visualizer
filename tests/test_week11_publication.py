import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import verify_week11_publication as g


class PublicationTests(unittest.TestCase):
    def setup_manifest(self):
        return g.verify_package()[0]

    def approvals(self):
        manifest = self.setup_manifest()
        request, request_sha = g.control_request(manifest,g.sha(g.BASE/'controls.json'))
        instant = g.clock('2026-11-20T00:00:00+00:00')
        head, pr_head = 'a'*40,'b'*40
        flags = {k:'false' for k in g.previous.FLAGS}
        env = dict(GITHUB_SHA=head,INSTALLATION_HEAD=head,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',
                   GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='racingmodel',GITHUB_TRIGGERING_ACTOR='racingmodel',GITHUB_REPOSITORY=g.REPO,
                   REQUESTED_RELEASE=request['release_id'],REQUESTED_SHA=request_sha,APPROVAL_COMMENT_ID='99')|flags
        grant = {k:request[k] for k in ('repository','destination','release_id','manifest_sha256',
                                       'scope','publication_deadline_utc','unattended')}
        grant.update(g.RECOVERY)
        grant.update(decision='APPROVED',installation_head=head,request_sha256=request_sha,
                     expires_at_utc='2026-11-20T00:30:00+00:00')
        ap = {'id':99,'user':{'login':'racingmodel'},'author_association':'OWNER','created_at':'2026-11-19T23:30:00Z',
              'updated_at':'2026-11-19T23:30:00Z','issue_url':f'https://api.github.com/repos/{g.REPO}/issues/999'}
        pr = {'merged':True,'merged_at':'2026-11-19T22:00:00Z','merge_commit_sha':head,
              'base':{'ref':'main','repo':{'full_name':g.REPO}},
              'head':{'ref':g.BRANCH,'sha':pr_head,'repo':{'full_name':g.REPO}}}
        def data(rel):
            return {
                'git/ref/heads/main':{'object':{'sha':head}},
                'issues/comments/99':ap,
                'pulls/999':pr,
                'commits/'+head:{'parents':[{'sha':g.PARENT},{'sha':pr_head}]},
            }[rel]
        return request,request_sha,instant,env,grant,ap,pr,data

    def test_valid_exact_package_and_review_request(self):
        manifest, parent=g.verify_package()
        self.assertEqual(len(parent),431)
        self.assertEqual(len(manifest['files']),440)
        self.assertEqual(len(manifest['frozen_parent_files']),425)
        g.control_request(manifest,g.sha(g.BASE/'controls.json'))

    def test_changed_assets_and_controls_block(self):
        actual=g.previous.inventory
        for target in ('week10_release/site','week11_release/overlay'):
            for operation in ('add','replace','remove'):
                with self.subTest(target=target,operation=operation):
                    def tamper(p):
                        fields = actual(p)
                        if p.as_posix()==target:
                            entry=next(iter(fields))
                            if operation=='add':
                                fields['extra']='a'*64
                            elif operation=='replace':
                                fields[entry]='a'*64
                            else:
                                del fields[entry]
                        return fields
                    with patch.object(g.previous,'inventory',tamper),self.assertRaises(ValueError):
                        g.verify_package()
        with self.assertRaises(ValueError):
            g.control_request(self.setup_manifest(),'a'*64)

    def test_both_windows_and_unix_approval(self):
        request,h,now,env,grant,ap,_,api=self.approvals()
        fence='\x60'*3
        for end in ('\n','\r\n'):
            ap['body']=end.join([fence+'json',json.dumps(grant),fence])
            with self.subTest(newline=end),patch.object(g,'api',api):
                self.assertEqual(g.approval(env,request,h,now)[1],g.clock(grant['expires_at_utc']))
        with self.assertRaises(ValueError):
            g.parse_approval(fence+'json\r{"wrong":true}\r'+fence)
        with self.assertRaises(ValueError):
            g.parse_approval(fence+'json\n{"scope":1,"scope":2}\n'+fence)

    def test_old_scope_edited_nonowners_expiration_and_wrong_commit(self):
        request,h,now,env,grant,ap,pr,api=self.approvals()
        fence='\x60'*3
        for key,replacement in [('scope','WEEK10_MIXED_ARCHIVAL_REVIEW_RECOVERY_STATIC_ONCE'),
                                ('recovery_id','replacement'),('prior_failed_run_id',1),
                                ('request_sha256','z'*64),('manifest_sha256','x'*64),
                                ('expires_at_utc','2026-11-19T23:45:00Z'),('expires_at_utc','2026-11-20T01:15:00Z'),
                                ('installation_head','z'*40),('unattended',0)]:
            ap['body']=fence+'json\n'+json.dumps(grant|{key:replacement})+'\n'+fence
            with self.subTest(key=key,value=replacement),patch.object(g,'api',api),self.assertRaises(ValueError):
                g.approval(env,request,h,now)
        ap['body']=fence+'json\n'+json.dumps(grant)+'\n'+fence
        for key,replacement in [('author_association','MEMBER'),('user',{'login':'other'}),('updated_at','2026-11-19T23:30:01Z')]:
            before=ap[key]
            ap[key]=replacement
            with self.subTest(key=key),patch.object(g,'api',api),self.assertRaises(ValueError):
                g.approval(env,request,h,now)
            ap[key]=before
        with patch.object(g,'api',api),self.assertRaises(ValueError):
            g.approval(env|{'INSTALLATION_HEAD':'c'*40},request,h,now)
        with patch.object(g,'api',api),self.assertRaises(ValueError):
            g.approval(env|{'GITHUB_RUN_ATTEMPT':'2'},request,h,now)
        with patch.object(g,'api',api),self.assertRaisesRegex(ValueError,'approval was consumed'):
            g.approval(env|{'APPROVAL_COMMENT_ID':str(g.CONSUMED_COMMENT)},request,h,now)
        pr['merge_commit_sha']='c'*40
        with patch.object(g,'api',api),self.assertRaises(ValueError):
            g.approval(env,request,h,now)

    def test_first_dispatch_repeat_failed_review_and_competitors(self):
        request,_,_,env,_,_,_,_=self.approvals()
        env['GITHUB_RUN_ID']='987'
        approved=g.clock('2026-11-19T23:30:00Z')
        owner={'login':'racingmodel'}
        current={'id':987,'workflow_id':int(g.WORKFLOW_ID),'head_sha':env['GITHUB_SHA'],
                 'created_at':'2026-11-19T23:50:00Z','conclusion':None,'status':'in_progress',
                 'event':'workflow_dispatch','run_attempt':1,'head_branch':'main',
                 'repository':{'full_name':g.REPO},'actor':owner,'triggering_actor':owner,
                 'path':'.github/workflows/manual-frontend-correction.yml'}
        old=current|{'id':g.PARENT_RUN,'status':'completed','head_sha':'d'*40,'conclusion':'success',
                     'created_at':'2026-11-13T12:30:00Z'}
        def verify(repository,workflow):
            def list_runs(endpoint):
                return repository if endpoint=='actions/runs' else workflow
            with patch.object(g,'api',return_value=current),patch.object(g,'verify_review_failure'),patch.object(g.previous,'all_run_records',list_runs):
                return g.dispatch_inventory(env,request,approved)
        prior=current|{'id':g.RECOVERY['prior_failed_run_id'],'head_sha':g.PARENT,'status':'completed',
                       'conclusion':'failure','created_at':'2026-11-19T17:31:58Z'}
        self.assertEqual(verify([old,prior,current],[old,prior,current])['run_id'],987)
        with self.assertRaisesRegex(ValueError,'Unreviewed dispatch'):
            verify([old,current],[old,current])
        earlier=current|{'id':986,'status':'completed','conclusion':'failure','head_sha':'e'*40}
        for repo,workflow in [
            ([old],[old,current]),([old,current],[old]),
            ([old,earlier,current],[old,earlier,current]),
            ([old,earlier|{'head_sha':env['GITHUB_SHA']},current],[old,earlier|{'head_sha':env['GITHUB_SHA']},current]),
            ([old,earlier|{'workflow_id':42,'status':'queued'},current],[old,current]),
        ]:
            with self.subTest(repo=list(r['id'] for r in repo)),self.assertRaises(ValueError):
                verify(repo,workflow)

    def test_only_the_pinned_review_only_failure_is_recoverable(self):
        rid=g.RECOVERY['prior_failed_run_id']
        run=dict(id=rid,workflow_id=int(g.WORKFLOW_ID),check_suite_id=g.FAILED_SUITE,head_sha=g.PARENT,
                 status='completed',conclusion='failure',event='workflow_dispatch',run_attempt=1,
                 head_branch='main',path=g.WORKFLOW_PATH,repository={'full_name':g.REPO},
                 actor={'login':'racingmodel'},triggering_actor={'login':'racingmodel'})
        suite=dict(id=g.FAILED_SUITE,head_sha=g.PARENT,status='completed',conclusion='failure',app={'slug':'github-actions'})
        common=dict(run_id=rid,run_attempt=1,head_sha=g.PARENT,status='completed')
        review=dict(common,id=g.FAILED_JOBS['review'],name='review',conclusion='failure',steps=[
            dict(name='Verify new owner approval, frozen assets and fixture clocks',conclusion='failure'),
            dict(name='Run actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9',conclusion='skipped')])
        publish=dict(common,id=g.FAILED_JOBS['publish'],name='publish',conclusion='skipped',steps=[],runner_id=None)
        expected={f'actions/runs/{rid}':run,f'check-suites/{g.FAILED_SUITE}':suite,
                  f'actions/runs/{rid}/attempts/1/jobs':{'total_count':2,'jobs':[review,publish]},
                  f'actions/runs/{rid}/artifacts':{'total_count':0,'artifacts':[]},'deployments':[]}
        def use(values):
            return patch.object(g,'api',side_effect=lambda path,**_:values[path])
        with use(expected):
            g.verify_failed_records()
        for path, change in [
            (f'actions/runs/{rid}',run|{'run_attempt':2}),
            (f'check-suites/{g.FAILED_SUITE}',suite|{'conclusion':'success'}),
            (f'actions/runs/{rid}/attempts/1/jobs',{'total_count':2,'jobs':[review,publish|{'conclusion':'success'}]}),
            (f'actions/runs/{rid}/attempts/1/jobs',{'total_count':2,'jobs':[review,publish|{'runner_id':3}]}),
            (f'actions/runs/{rid}/artifacts',{'total_count':1,'artifacts':[{'name':'github-pages'}]}),
            ('deployments',[{'sha':g.PARENT}]),
        ]:
            with self.subTest(path=path,change=change),use(expected|{path:change}),self.assertRaises(ValueError):
                g.verify_failed_records()

    def test_cutoff_and_flex_from_frozen_sources(self):
        source_dir=os.environ.get('WEEK11_TEST_SOURCE_DIR')
        if source_dir is None:
            self.skipTest('Local captures are kept outside the public Git repository')
        root=Path(source_dir)
        schedule=(root/'games.csv').read_bytes()
        official=(root/'nfl_week11.html').read_bytes()
        m=self.setup_manifest()
        safe=g.clock(m['first_cutoff_utc'])-timedelta(seconds=1)
        self.assertEqual(g.fixture_check(schedule,official,m,safe)['fixtures'],13)
        for observed in (safe+timedelta(seconds=1),safe+timedelta(hours=1)):
            with self.assertRaises(ValueError):
                g.fixture_check(schedule,official,m,observed)
        with self.assertRaises(ValueError):
            g.fixture_check(schedule,official.replace(b'9:00 PM',b'8:00 PM').replace(b'colts-at-texans-2026-reg-11',b'bad-2026-reg-11'),m,safe)
        changed=copy.deepcopy(m)
        changed['fixture_cutoffs']['2026_11_IND_HOU']='2026-11-20T01:15:00+00:00'
        with self.assertRaises(ValueError):
            g.fixture_check(schedule,official,changed,safe)


if __name__=='__main__':
    unittest.main()
