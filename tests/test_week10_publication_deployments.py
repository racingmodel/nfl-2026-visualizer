"""Offline Week 10 publication controls; no approval, workflow or deployment writes."""
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from tempfile import TemporaryDirectory
import copy,csv,importlib.util,io,json,os,shutil,sys,unittest

ROOT=Path(os.environ.get('WEEK10_GUARD_ROOT',Path(__file__).resolve().parents[1])).resolve()
spec=importlib.util.spec_from_file_location('week10_guard_under_test',ROOT/'scripts/verify_week10_publication.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
BASE=ROOT/'week10_release'
MANIFEST=g.read(BASE/'publication_manifest.json')
FIXTURE=g.read(Path(__file__).parent/'fixtures/week10_publish_deployment.json')

class DeploymentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f=copy.deepcopy(FIXTURE)
        self.f['run'].update(status='in_progress',conclusion=None)
        self.f['job'].update(status='in_progress',conclusion=None)
        self.f['graphql']['node']['status']='IN_PROGRESS'
        self.env=dict(GITHUB_RUN_ID=str(self.f['run']['id']),GITHUB_JOB='publish',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=self.f['run']['head_sha'])
        self.extra=[]
    def api(self,path,**kwargs):
        rid=self.env['GITHUB_RUN_ID']
        if path=='actions/runs/'+rid:return self.f['run']
        if path==f'actions/runs/{rid}/attempts/1/jobs':return dict(total_count=1,jobs=[self.f['job']])
        if path.startswith('deployments/') and path.endswith('/statuses'):
            return self.extra if path.split('/')[1]=='999' else [self.f['in_progress_status']]
        raise AssertionError('Unexpected mocked route '+path)
    def verify(self,rows=None,phase='pre-deploy'):
        with patch.object(g,'api',side_effect=self.api),patch.object(g,'graphql',return_value=self.f['graphql']):
            return g.verify_deployments(rows if rows is not None else [self.f['deployment']],self.env,phase)
    def rejected(self,rows=None,phase='pre-deploy'):
        with self.assertRaises((ValueError,KeyError,RuntimeError)):self.verify(rows,phase)
    def test_actual_own_record_allowed_via_checkrun_edge(self):
        self.assertEqual(self.verify()['authenticated_current_deployment_ids'],[7738461993])
    def test_payload_or_matching_sha_cannot_prove_identity(self):
        self.f['graphql']['node']['deployment']=None
        self.f['deployment']['payload']={'run_id':43226774165,'workflow_id':399724330}
        self.rejected()
    def test_same_sha_other_run_deployment_blocked(self):
        self.f['deployment']['id']=999;self.extra=[self.f['in_progress_status']]
        self.rejected()
    def test_same_environment_other_run_status_blocked(self):
        self.f['in_progress_status']['log_url']=self.f['job']['html_url'].replace('43226774165','43226774166')
        self.rejected()
    def test_unrelated_active_record_alongside_own_blocked(self):
        other=copy.deepcopy(self.f['deployment']);other['id']=999
        self.extra=[dict(self.f['in_progress_status'],state='in_progress')]
        self.rejected([self.f['deployment'],other])
    def test_forged_metadata_and_urls_without_server_edge_blocked(self):
        self.f['graphql']['node']['deployment']['databaseId']=999
        self.rejected()
    def test_wrong_checkrun_or_suite_or_app_blocked(self):
        variants=[('databaseId',1),('status','QUEUED'),('checkSuite',dict(databaseId=1,app=dict(databaseId=15368,slug='github-actions'))),
                  ('checkSuite',dict(databaseId=self.f['run']['check_suite_id'],app=dict(databaseId=1,slug='forged')))]
        for key,value in variants:
            with self.subTest(key=key,value=value):
                old=copy.deepcopy(self.f['graphql']);self.f['graphql']['node'][key]=value;self.rejected();self.f['graphql']=old
    def test_missing_or_inaccessible_graphql_fails_closed(self):
        with patch.object(g,'api',side_effect=self.api),patch.object(g,'graphql',side_effect=RuntimeError('denied')):
            with self.assertRaises(RuntimeError):g.verify_deployments([self.f['deployment']],self.env,'pre-deploy')
        self.f['graphql']={'node':None};self.rejected()
    def test_wrong_runtime_run_workflow_actor_or_attempt_blocked(self):
        for key,value in [('id',1),('workflow_id',1),('run_attempt',2),('head_sha','0'*40),('actor',dict(login='other')),
                          ('status','completed'),('event','push'),('repository',dict(full_name='other/repo'))]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.f['run']);self.f['run'][key]=value;self.rejected();self.f['run']=old
    def test_job_identity_ambiguity_or_forged_node_blocked(self):
        for key,value in [('run_id',1),('run_attempt',2),('head_sha','0'*40),('node_id','forged'),('status','completed'),('check_run_url','https://example.com/check')]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.f['job']);self.f['job'][key]=value;self.rejected();self.f['job']=old
        with patch.object(g,'api',return_value=self.f['run']):
            # A missing job API shape fails closed, never grants an exception.
            with self.assertRaises(ValueError):g.current_publish_deployment(self.env)
    def test_missing_or_review_job_environment_identity_blocked(self):
        self.env['GITHUB_JOB']='review';self.rejected()
    def test_deployment_metadata_mismatch_blocked(self):
        for key,value in [('sha','0'*40),('ref','other'),('environment','other'),('node_id','forged'),('task','other'),
                          ('performed_via_github_app',dict(id=1,slug='github-actions'))]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.f['deployment']);self.f['deployment'][key]=value;self.rejected();self.f['deployment']=old
    def test_status_urls_and_environment_must_match(self):
        for key in ('environment','deployment_url','log_url','target_url'):
            with self.subTest(key=key):
                old=copy.deepcopy(self.f['in_progress_status']);self.f['in_progress_status'][key]='forged';self.rejected();self.f['in_progress_status']=old
    def test_unknown_queued_and_empty_status_fail_closed(self):
        for state in ('queued','pending','unknown',''):
            self.f['in_progress_status']['state']=state;self.rejected()
        with patch.object(g,'api',side_effect=lambda path,**kw:[] if path.startswith('deployments/') else self.api(path,**kw)),patch.object(g,'graphql',return_value=self.f['graphql']):
            with self.assertRaises(ValueError):g.verify_deployments([self.f['deployment']],self.env,'pre-deploy')
    def test_review_does_not_allow_any_current_deployment(self):self.rejected(phase='review')
    def test_missing_or_multiple_current_deployments_blocked(self):
        self.rejected([]);self.rejected([self.f['deployment'],self.f['deployment']])
    def test_earlier_completed_and_failed_records_allowed(self):
        for state in ('success','failure','error','inactive'):
            self.f['in_progress_status']['state']=state
            self.assertEqual(self.verify(phase='review')['authenticated_current_deployment_ids'],[])
    def test_old_failure_reproduced_and_new_guard_accepts_exact_lifecycle(self):
        self.assertNotIn(self.f['in_progress_status']['state'],{'success','failure','error','inactive'})
        self.assertEqual(self.verify()['authenticated_current_deployment_ids'],[7738461993])
    def pipeline(self,wrong_main=False):
        instant=g.clock(g.read(BASE/'review_request.json')['created_at_utc'])+timedelta(minutes=2)
        games=g.read(BASE/'site/data/research/2026-w10.json')['games'];rows=[]
        for game in games:
            stamp=g.clock(game['kickoff_utc']).astimezone(g.ZoneInfo('America/New_York'))
            rows.append(dict(season='2026',week='10',game_type='REG',game_id=game['game_id'],away_team=game['away_team'],home_team=game['home_team'],
                gameday=stamp.strftime('%Y-%m-%d'),gametime=stamp.strftime('%H:%M'),away_score='',home_score=''))
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
        def api(path,**kw):
            if path=='git/ref/heads/main':return dict(object=dict(sha='0'*40 if wrong_main else self.env['GITHUB_SHA']))
            if path=='actions/runs':return dict(workflow_runs=[self.f['run']] if kw.get('status')=='in_progress' else [])
            if path=='pages':return dict(build_type='workflow')
            if path=='deployments':return [self.f['deployment']]
            if path.startswith('actions/workflows/'):return dict(total_count=0,workflow_runs=[])
            return self.api(path,**kw)
        with TemporaryDirectory(prefix='w10p-',dir=Path.cwd()) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(Path.cwd().resolve()))
            output=Path(temp)/'check.json'
            with patch.dict(os.environ,self.env,clear=True),patch.object(sys,'argv',['guard','pre-deploy','--out',str(output)]),\
                 patch.object(g,'BASE',BASE),patch.object(g,'package',return_value=MANIFEST),\
                 patch.object(g,'authority',return_value=(instant-timedelta(minutes=1),instant+timedelta(minutes=30))),\
                 patch.object(g,'now',return_value=instant),patch.object(g,'api',side_effect=api),\
                 patch.object(g,'graphql',return_value=self.f['graphql']),patch.object(g,'download',return_value=stream.getvalue().encode()),\
                 patch.object(g,'verify_live_parent',return_value=[]):
                g.main()
            return g.read(output)
    def test_actual_predeploy_entrypoint_accepts_reconstructed_job_start(self):
        self.assertEqual(self.pipeline()['deployment_check']['authenticated_current_deployment_ids'],[7738461993])
    def test_actual_entrypoint_still_rejects_changed_main(self):
        with self.assertRaisesRegex(ValueError,'main moved'):self.pipeline(wrong_main=True)
    def test_graphql_transport_rejects_errors_or_missing_data(self):
        for body in ({'errors':[{'message':'denied'}]}, {'data':None}):
            with self.subTest(body=body),patch.dict(os.environ,{'GH_TOKEN':'test-only-not-a-credential'}),\
                 patch.object(g,'urlopen',return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(ValueError):g.graphql(g.CHECK_RUN_DEPLOYMENT_QUERY,{'id':'fixture'})

class ExistingAuthorityAndAssetTests(unittest.TestCase):
    def setUp(self):
        self.instant=g.clock(g.read(BASE/'review_request.json')['created_at_utc'])+timedelta(minutes=2)
        self.head='f'*40
        self.env=dict(GITHUB_REPOSITORY=g.REPO,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',
            GITHUB_ACTOR='racingmodel',GITHUB_TRIGGERING_ACTOR='racingmodel',GITHUB_SHA=self.head,INSTALLATION_HEAD=self.head,
            REQUESTED_RELEASE=g.RELEASE_ID,REQUESTED_SHA=g.sha(BASE/'review_request.json'),CONTROL_SHA256=g.sha(BASE/'controls.json'),APPROVAL_COMMENT_ID='999')
        self.env.update({flag:'false' for flag in g.FLAGS})
        self.grant=dict(g.read(BASE/'review_request.json'),decision='APPROVED',request_sha256=g.sha(BASE/'review_request.json'),
            installation_head=self.head,expires_at_utc=(self.instant+timedelta(minutes=30)).isoformat())
        stamp=(self.instant-timedelta(minutes=1)).isoformat()
        self.comment=dict(id=999,user=dict(login='racingmodel'),author_association='OWNER',created_at=stamp,updated_at=stamp,
            issue_url='https://api.github.com/repos/'+g.REPO+'/issues/999',html_url='https://github.com/'+g.REPO+'/pull/999#issuecomment-999')
        self.pr=dict(merged=True,merge_commit_sha=self.head,merged_at=g.read(BASE/'review_request.json')['created_at_utc'],
            base=dict(ref='main',repo=dict(full_name=g.REPO)),head=dict(ref=g.REVIEW_BRANCH,repo=dict(full_name=g.REPO)))
    def authority(self):
        comment=dict(self.comment,body='```json\n'+json.dumps(self.grant)+'\n```')
        with patch.object(g,'api',side_effect=lambda path,**kw:comment if path.startswith('issues/comments/') else self.pr):
            return g.authority(MANIFEST,self.env,self.instant,BASE)
    def test_new_exact_package_and_synthetic_authority_accepted(self):
        self.assertEqual(g.package(BASE)['release_id'],g.RELEASE_ID);self.assertGreater(self.authority()[1],self.instant)
    def test_previous_consumed_approval_and_request_rejected(self):
        self.grant.update(request_sha256='5886b999548c2e19390d067eccc24ab24bddec5cfb78a7e44abfc5468f672210',installation_head='9fc569f07af90182b77e7510267411222eed338f')
        with self.assertRaises(ValueError):self.authority()
    def test_stale_edited_expired_or_wrong_scope_approval_rejected(self):
        for key,value in [('expires_at_utc',self.instant.isoformat()),('scope','WEEK9_TIDY_EXACT_STATIC_ONCE'),('release_id','other'),
                          ('manifest_sha256','0'*64),('request_sha256','0'*64)]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.grant);self.grant[key]=value
                with self.assertRaises(ValueError):self.authority()
                self.grant=old
        self.comment['updated_at']=self.instant.isoformat()
        with self.assertRaises(ValueError):self.authority()
    def test_wrong_installation_or_actor_or_rerun_rejected(self):
        for key,value in [('INSTALLATION_HEAD','0'*40),('GITHUB_ACTOR','other'),('GITHUB_RUN_ATTEMPT','2'),('RESEARCH_PUBLISH_ENABLED','true')]:
            with self.subTest(key=key):
                old=dict(self.env);self.env[key]=value
                with self.assertRaises(ValueError):self.authority()
                self.env=old
    def test_wrong_request_pin_and_control_bytes_rejected(self):
        self.env['REQUESTED_SHA']='0'*64
        with self.assertRaises(ValueError):self.authority()
        self.env['REQUESTED_SHA']=g.sha(BASE/'review_request.json');self.env['CONTROL_SHA256']='0'*64
        with self.assertRaises(ValueError):self.authority()
    def test_changed_and_extra_assets_rejected(self):
        with TemporaryDirectory(prefix='w10t-',dir=Path.cwd()) as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to(Path.cwd().resolve()))
            site=Path(temp);shutil.copytree(BASE,site/'week10_release');base=site/'week10_release'
            asset=base/'site/index.html';original=asset.read_bytes();asset.write_bytes(original+b'changed')
            with self.assertRaises(ValueError):g.package(base)
            asset.write_bytes(original);(base/'site/extra.js').write_bytes(b'added')
            with self.assertRaises(ValueError):g.package(base)
    def test_repeat_dispatch_remains_rejected(self):
        with patch.object(g,'api',return_value=dict(total_count=1,workflow_runs=[dict(id=2,head_sha=self.head)])):
            with self.assertRaises(ValueError):g.check_first_dispatch(dict(GITHUB_SHA=self.head,GITHUB_RUN_ID='1'),'2026-11-12T00:00Z')
    def test_historical_assets_and_parent_remain_exact(self):
        parent=MANIFEST['previous_live_release']['files']
        history={n:h for n,h in parent.items() if n.startswith('data/') and n not in {'data/hosted_catalog.json','data/td_catalog.json','data/quarter_catalog.json'}}
        self.assertEqual(len(history),57);self.assertTrue(all(MANIFEST['files'][n]==h for n,h in history.items()))
        bad=copy.deepcopy(MANIFEST);bad['previous_live_release']['main']='0'*40
        with self.assertRaises(ValueError):g.exact_site_scope(bad)
    def test_cutoff_and_live_fixture_rejection(self):
        games={v['game_id']:v for v in g.read(BASE/'site/data/research/2026-w10.json')['games']}
        rows=[]
        for game in games.values():
            stamp=g.clock(game['kickoff_utc']).astimezone(g.ZoneInfo('America/New_York'))
            rows.append(dict(season='2026',week='10',game_type='REG',game_id=game['game_id'],away_team=game['away_team'],home_team=game['home_team'],
                gameday=stamp.strftime('%Y-%m-%d'),gametime=stamp.strftime('%H:%M'),away_score='',home_score='',game_status=''))
        def raw():
            s=io.StringIO();w=csv.DictWriter(s,fieldnames=rows[0]);w.writeheader();w.writerows(rows);return s.getvalue().encode()
        g.cutoff_check(raw(),MANIFEST,self.instant,games)
        with self.assertRaises(ValueError):g.cutoff_check(raw(),MANIFEST,g.clock(MANIFEST['first_cutoff_utc']),games)
        rows[0]['game_status']='LIVE'
        with self.assertRaises(ValueError):g.cutoff_check(raw(),MANIFEST,self.instant,games)
    def test_workflow_has_only_manual_trigger_and_required_read_permissions(self):
        text=(ROOT/'.github/workflows/manual-frontend-correction.yml').read_text(encoding='utf-8')
        self.assertIn('  workflow_dispatch:',text)
        for trigger in ('push','pull_request','schedule','workflow_run'):self.assertNotIn('\n  '+trigger+':',text)
        self.assertIn('      checks: read',text);self.assertIn('      deployments: read',text)
        self.assertIn('      name: github-pages',text)

if __name__=='__main__':unittest.main(verbosity=2)
