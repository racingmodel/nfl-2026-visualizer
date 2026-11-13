"""Offline/synthetic release-security tests. No real approval or GitHub writes."""
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from tempfile import TemporaryDirectory
import copy,csv,importlib.util,io,json,os,shutil,sys,unittest

ROOT=Path(os.environ.get('WEEK10_GUARD_ROOT',Path(__file__).resolve().parents[1])).resolve()
spec=importlib.util.spec_from_file_location('recovery_guard_tested',ROOT/'scripts/verify_week10_publication.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
BASE=ROOT/'week10_release';M=g.read(BASE/'publication_manifest.json');Q=g.read(BASE/'review_request.json')
GAMES={r['game_id']:r for r in g.read(BASE/'site/data/research/2026-w10.json')['games']}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.instant=g.clock(Q['created_at_utc'])+timedelta(minutes=2)
        self.head='c'*40;self.asset_merge='a'*40;self.control_head='b'*40;self.run_id=999001
        self.env=dict(GITHUB_REPOSITORY=g.REPO,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',
            GITHUB_ACTOR='racingmodel',GITHUB_TRIGGERING_ACTOR='racingmodel',GITHUB_SHA=self.head,INSTALLATION_HEAD=self.head,
            REQUESTED_RELEASE=g.RELEASE_ID,REQUESTED_SHA=g.sha(BASE/'review_request.json'),CONTROL_SHA256=g.sha(BASE/'controls.json'),
            APPROVAL_COMMENT_ID='9876543210',GITHUB_RUN_ID=str(self.run_id),GITHUB_JOB='review',**{f:'false' for f in g.FLAGS})
        self.grant=dict(decision='APPROVED',repository=g.REPO,destination=g.SITE,release_id=g.RELEASE_ID,manifest_sha256=g.MANIFEST_SHA256,
            request_sha256=self.env['REQUESTED_SHA'],installation_head=self.head,scope=g.APPROVAL_SCOPE,
            expires_at_utc=(self.instant+timedelta(hours=1)).isoformat(),unattended=False,publication_mode='MIXED_ARCHIVE_AND_PREGAME',
            archived_fixture_ids=['2026_10_WAS_NYG'],publication_deadline_utc=M['archival_policy']['publication_deadline_utc'])
        self.comment=dict(id=9876543210,user=dict(login='racingmodel'),author_association='OWNER',
            created_at=(self.instant-timedelta(minutes=1)).isoformat(),updated_at=(self.instant-timedelta(minutes=1)).isoformat(),
            issue_url='https://api.github.com/repos/'+g.REPO+'/issues/99999',html_url='https://github.com/'+g.REPO+'/pull/99999#issuecomment-9876543210')
        self.asset_pr=dict(merged=True,merge_commit_sha=self.asset_merge,head=dict(sha=Q['asset_pr_head_sha'],ref=Q['asset_review_branch'],repo=dict(full_name=g.REPO)),base=dict(ref='main',repo=dict(full_name=g.REPO)))
        self.control_pr=dict(merged=True,merge_commit_sha=self.head,merged_at=(self.instant-timedelta(seconds=90)).isoformat(),head=dict(sha=self.control_head,ref=g.REVIEW_BRANCH,repo=dict(full_name=g.REPO)),base=dict(ref='main',repo=dict(full_name=g.REPO)))
        self.current=dict(id=self.run_id,workflow_id=int(g.WORKFLOW_ID),head_sha=self.head,event='workflow_dispatch',run_attempt=1,head_branch='main',status='in_progress',conclusion=None,repository=dict(full_name=g.REPO),actor=dict(login='racingmodel'),triggering_actor=dict(login='racingmodel'))
        self.cleared=dict(self.current,id=43257307848,head_sha=g.BASE_MAIN_SHA,check_suite_id=117139170263,status='completed',conclusion='cancelled')
        self.repo=[self.cleared,self.current];self.workflow=list(self.repo)
        self.workflows=[dict(id=int(g.WORKFLOW_ID),state='active')]+[dict(id=i,state='disabled_manually') for i in range(4)]
        self.suite=dict(id=117139170263,status='completed',conclusion='cancelled')
        self.records=[]
        for game in GAMES.values():
            t=g.clock(game['kickoff_utc']).astimezone(g.ZoneInfo('America/New_York'));archived=game['game_id']=='2026_10_WAS_NYG'
            self.records.append(dict(season='2026',week='10',game_type='REG',game_id=game['game_id'],away_team=game['away_team'],home_team=game['home_team'],
                gameday=t.strftime('%Y-%m-%d'),gametime=t.strftime('%H:%M'),away_score='7' if archived else '',home_score='10' if archived else '',status='FINAL' if archived else 'SCHEDULED'))
    def raw(self):
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=self.records[0]);w.writeheader();w.writerows(self.records);return out.getvalue().encode()
    def api(self,path,**kw):
        # Deliberately reject filter use: a filtered zero may never grant authority.
        if path in ('actions/runs','actions/workflows/'+g.WORKFLOW_ID+'/runs'):
            self.assertTrue(set(kw)<={'per_page','page'});rows=self.repo if path=='actions/runs' else self.workflow
            return dict(total_count=len(rows),workflow_runs=copy.deepcopy(rows))
        if path=='actions/runs/43257307848':return self.cleared
        if path=='check-suites/117139170263':return self.suite
        if path=='actions/runs/43257307848/jobs':return dict(total_count=0,jobs=[])
        if path=='actions/runs/'+str(self.run_id):return self.current
        if path=='issues/comments/9876543210':return dict(self.comment,body='```json\n'+json.dumps(self.grant)+'\n```')
        if path=='pulls/99999':return self.control_pr
        if path=='pulls/'+str(Q['asset_pr_number']):return self.asset_pr
        if path=='commits/'+self.asset_merge:return dict(parents=[dict(sha=g.BASE_MAIN_SHA),dict(sha=Q['asset_pr_head_sha'])])
        if path=='commits/'+self.head:return dict(parents=[dict(sha=self.asset_merge),dict(sha=self.control_head)])
        if path=='git/ref/heads/main':return dict(object=dict(sha=self.head))
        if path=='actions/workflows':return dict(total_count=5,workflows=self.workflows)
        if path=='actions/variables':return dict(total_count=4,variables=[dict(name=f,value='false') for f in g.FLAGS])
        if path=='pages':return dict(build_type='workflow')
        if path=='deployments':return [dict(id=7738461993)]
        if path=='deployments/7738461993/statuses':return [dict(state='failure')]
        raise AssertionError('Unexpected offline API route '+path)
    def authority(self):
        with patch.object(g,'api',side_effect=self.api):return g.authority(M,self.env,self.instant,BASE)
    def inventory(self):
        with patch.object(g,'api',side_effect=self.api):return g.check_first_dispatch(self.env,Q['created_at_utc'])
    def test_exact_431_asset_package_and_archive_contract(self):self.assertEqual(g.package(BASE)['release_id'],g.RELEASE_ID)
    def test_legacy_pregame_scope_still_rejects_closed_game(self):
        with self.assertRaisesRegex(ValueError,'closed'):g.cutoff_check(self.raw(),M,self.instant,GAMES)
    def test_new_scope_accepts_one_frozen_archive_and_13_pregame(self):
        result=g.publication_clock_check(self.raw(),M,self.instant,GAMES);self.assertEqual(result['archive_only'],['2026_10_WAS_NYG']);self.assertEqual(len(result['strict_pregame']),13)
    def test_no_additional_archived_fixture(self):
        m=copy.deepcopy(M);m['archival_policy']['archived_fixture_ids'].append('2026_10_NE_DET')
        with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),m,self.instant,GAMES)
    def test_remaining_live_final_ambiguous_or_score_fails_closed(self):
        for state in ['LIVE','FINAL','UNKNOWN','PENDING']:
            with self.subTest(state=state):
                old=self.records[1]['status'];self.records[1]['status']=state
                with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),M,self.instant,GAMES)
                self.records[1]['status']=old
        self.records[1]['home_score']='3'
        with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),M,self.instant,GAMES)
    def test_original_remaining_cutoff_cannot_be_extended(self):
        with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),M,g.clock(M['archival_policy']['publication_deadline_utc']),GAMES)
        m=copy.deepcopy(M);m['archival_policy']['publication_deadline_utc']='2026-11-18T00:00:00Z'
        with self.assertRaises(ValueError):g.archive_contract(m,BASE)
    def test_wrong_schedule_identity_or_missing_game_blocked(self):
        for key,value in [('home_team','DAL'),('week','11'),('gametime','21:15')]:
            with self.subTest(key=key):
                original=self.records[1][key];self.records[1][key]=value
                with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),M,self.instant,GAMES)
                self.records[1][key]=original
    def test_archive_cannot_be_new_post_cutoff_forecast(self):
        games=copy.deepcopy(GAMES);games['2026_10_WAS_NYG']['created_at_utc']=self.instant.isoformat()
        with self.assertRaises(ValueError):g.publication_clock_check(self.raw(),M,self.instant,games)
    def test_changed_descriptor_or_historical_pins_rejected(self):
        for key in ['descriptor_sha256','mode']:
            m=copy.deepcopy(M);m['archival_policy'][key]='wrong'
            with self.assertRaises(ValueError):g.archive_contract(m,BASE)
        m=copy.deepcopy(M);m['frozen_data_hashes']['data/model_c_week10.json']='wrong'
        with self.assertRaises(ValueError):g.archive_contract(m,BASE)
    def test_fresh_authenticated_new_scope_authority(self):self.assertGreater(self.authority()[1],self.instant)
    def test_old_scope_wrong_archive_and_wrong_release_rejected(self):
        for key,value in [('scope','WEEK10_FINAL_MODEL_C_STATIC_ONCE'),('scope','WEEK9_TIDY_STATIC_ONCE'),('release_id','old'),('manifest_sha256','wrong'),('request_sha256','wrong'),('archived_fixture_ids',[]),('publication_mode','PREGAME_ONLY')]:
            with self.subTest(key=key,value=value):
                saved=self.grant[key];self.grant[key]=value
                with self.assertRaises((ValueError,KeyError)):self.authority()
                self.grant[key]=saved
    def test_expired_future_and_overlong_approval_rejected(self):
        for value in [self.instant.isoformat(),(self.instant-timedelta(hours=1)).isoformat(),(self.instant+timedelta(hours=3)).isoformat()]:
            self.grant['expires_at_utc']=value
            with self.assertRaises(ValueError):self.authority()
    def test_edited_wrong_owner_or_wrong_pr_approval_rejected(self):
        for key,value in [('updated_at',self.instant.isoformat()),('author_association','CONTRIBUTOR'),('user',dict(login='other'))]:
            original=self.comment[key];self.comment[key]=value
            with self.assertRaises(ValueError):self.authority()
            self.comment[key]=original
        self.control_pr['head']['ref']='old-week9-branch'
        with self.assertRaises(ValueError):self.authority()
    def test_approval_requires_actual_merged_commit_and_asset_parent(self):
        for target,key,value in [(self.control_pr,'merged',False),(self.control_pr,'merge_commit_sha','0'*40),(self.asset_pr,'merged',False)]:
            saved=target[key];target[key]=value
            with self.assertRaises(ValueError):self.authority()
            target[key]=saved
        self.asset_pr['head']['sha']='0'*40
        with self.assertRaises(ValueError):self.authority()
    def test_wrong_actor_commit_attempt_flags_and_controls_rejected(self):
        for key,value in [('GITHUB_ACTOR','other'),('INSTALLATION_HEAD','0'*40),('GITHUB_RUN_ATTEMPT','2'),('CONTROL_SHA256','wrong'),('REQUESTED_SHA','wrong'),(g.FLAGS[0],'true')]:
            saved=self.env[key];self.env[key]=value
            with self.assertRaises((ValueError,KeyError)):self.authority()
            self.env[key]=saved
    def test_unfiltered_and_direct_inventory_permits_only_original_attempt(self):self.assertEqual(self.inventory()['direct_current_run'],self.run_id)
    def test_second_dispatch_is_rejected_even_if_failed_and_filters_omit_it(self):
        other=dict(self.current,id=self.run_id+1,status='completed',conclusion='failure');self.repo.append(other);self.workflow.append(other)
        with self.assertRaisesRegex(ValueError,'One-time'):self.inventory()
    def test_direct_run_missing_from_unfiltered_history_fails_closed(self):
        self.workflow=[self.cleared]
        with self.assertRaises(ValueError):self.inventory()
    def test_other_unfinished_workflow_fails_closed(self):
        self.repo.append(dict(self.current,id=12345,workflow_id=1,head_sha='e'*40,status='queued'))
        with self.assertRaisesRegex(ValueError,'Other repository'):self.inventory()
    def test_uncleared_original_run_suite_or_new_jobs_rejected(self):
        self.cleared['status']='queued'
        with self.assertRaises(ValueError):self.inventory()
        self.cleared['status']='completed';self.suite['conclusion']=None
        with self.assertRaises(ValueError):self.inventory()
    def test_wrong_current_run_actor_attempt_or_commit_rejected(self):
        for key,value in [('head_sha','e'*40),('run_attempt',2),('actor',dict(login='other'))]:
            saved=self.current[key];self.current[key]=value
            with self.assertRaises(ValueError):self.inventory()
            self.current[key]=saved
    def test_ambiguous_or_incomplete_inventory_rejected(self):
        with patch.object(g,'api',return_value=dict(total_count=2,workflow_runs=[self.current])):
            with self.assertRaises(ValueError):g.all_run_records('actions/runs')
    def test_only_manual_workflow_may_be_enabled(self):
        with patch.object(g,'api',side_effect=self.api):g.verify_dispatch_workflow_states()
        self.workflows[1]['state']='active'
        with patch.object(g,'api',side_effect=self.api),self.assertRaises(ValueError):g.verify_dispatch_workflow_states()
        self.workflows=[dict(id=i,state='disabled_manually') for i in range(5)]
        with patch.object(g,'api',side_effect=self.api),self.assertRaises(ValueError):g.verify_dispatch_workflow_states()
    def test_changed_or_extra_assets_fail_real_package_inventory(self):
        temp_root=Path(os.environ.get('WEEK10_TEST_TMP_ROOT',ROOT/'.tmp'));temp_root.mkdir(exist_ok=True,parents=True)
        for name in ['data/model_c_week10.json','data/original_td_week10.json','data/week10_publication_context.json','extra.js']:
            with TemporaryDirectory(prefix='w10r-',dir=temp_root) as tmp:
                target=Path(tmp).resolve();self.assertTrue(target.is_relative_to(temp_root.resolve()))
                shutil.copytree(BASE,target/'week10_release');p=target/'week10_release/site'/name;p.write_bytes(b'changed')
                with self.assertRaises(ValueError):g.package(target/'week10_release')
    def test_actual_review_entrypoint_in_new_scope(self):
        temp_root=Path(os.environ.get('WEEK10_TEST_TMP_ROOT',ROOT/'.tmp'));temp_root.mkdir(exist_ok=True,parents=True)
        with TemporaryDirectory(prefix='w10r-',dir=temp_root) as tmp:
            output=Path(tmp)/'review.json';self.assertTrue(output.parent.resolve().is_relative_to(temp_root.resolve()))
            with patch.dict(os.environ,self.env,clear=True),patch.object(sys,'argv',['guard','review','--out',str(output)]),patch.object(g,'BASE',BASE),patch.object(g,'now',return_value=self.instant),patch.object(g,'api',side_effect=self.api),patch.object(g,'download',return_value=self.raw()),patch.object(g,'verify_live_parent',return_value=[]):g.main()
            self.assertEqual(g.read(output)['fixture_check']['archive_only'],['2026_10_WAS_NYG'])
    def test_manual_trigger_and_deployment_permissions_unchanged(self):
        source=(ROOT/'.github/workflows/manual-frontend-correction.yml').read_text(encoding='utf-8')
        self.assertIn('\n  workflow_dispatch:',source)
        for event in ['push','schedule','pull_request']:self.assertNotIn('\n  '+event+':',source)
        for permission in ['checks: read','deployments: read','pages: write','id-token: write']:self.assertIn(permission,source)
        review=source.split('  review:',1)[1].split('  publish:',1)[0]
        self.assertIn('checks: read',review);self.assertIn('deployments: read',review)
        self.assertNotIn("api('actions/variables'",(ROOT/'scripts/verify_week10_publication.py').read_text(encoding='utf-8'))

def load_tests(loader,tests,pattern):
    # Reuse the 19 unchanged authenticated self-deployment function regressions.
    # Its two old entrypoint mocks used the obsolete filtered-history contract;
    # the new entrypoint and inventory are exercised above instead.
    spec=importlib.util.spec_from_file_location('preserved_deployment_tests',ROOT/'tests/test_week10_publication_deployments.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    excluded={'test_actual_predeploy_entrypoint_accepts_reconstructed_job_start','test_actual_entrypoint_still_rejects_changed_main'}
    for name in loader.getTestCaseNames(old.DeploymentIdentityTests):
        if name not in excluded:tests.addTest(old.DeploymentIdentityTests(name))
    return tests

if __name__=='__main__':unittest.main(verbosity=2)
