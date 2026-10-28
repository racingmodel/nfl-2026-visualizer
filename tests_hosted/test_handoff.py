from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
import copy, io, json, os, sys, unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import supervised_hosted_publication as handoff
import supervised_release_data_gate as gate

class HandoffSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site=Path(os.environ.get('TEST_CANDIDATE','candidate-site'))
        cls.data=handoff.load(cls.site/'data/weekly_research.json')
        cls.prior=handoff.load(ROOT/'correction_release/site/data/weekly_research.json')
    def mutate(self,fn):
        d=copy.deepcopy(self.data);fn(d)
        with self.assertRaises((ValueError,KeyError)):gate.records(d,[self.prior],handoff.now())
    def test_exact_export(self): self.assertEqual(handoff.package(self.site)[2]['release_id'],'hosted-classic-276f9c25324e88b3')
    def test_unattended_record_absent(self):
        self.assertFalse(handoff.request()['unattended_authorized'])
    def test_original_issue_time(self): self.mutate(lambda d:d['games'][0].update(original_created_at_utc='2026-10-28T00:00:00Z'))
    def test_original_player_id(self): self.mutate(lambda d:d['games'][0]['players'][0].update(player_id='00-INVALID'))
    def test_wrong_opponent(self): self.mutate(lambda d:d['games'][0]['players'][0].update(opponent='XXX'))
    def test_uncertified_probability(self): self.mutate(lambda d:d['games'][0]['players'][0].update(anytime_td_probability=0.5))
    def test_unapproved_model(self): self.mutate(lambda d:d['games'][0].update(model_sha256='0'*64))
    def test_missing_forecast_field(self): self.mutate(lambda d:d['games'][0]['forecast'].pop('model_total'))
    def test_wrong_cutoff(self): self.mutate(lambda d:d['games'][0].update(cutoff_utc=d['games'][0]['kickoff_utc']))
    def env(self):
        return dict(GITHUB_ACTOR='racingmodel',GITHUB_TRIGGERING_ACTOR='racingmodel',GITHUB_REPOSITORY='racingmodel/nfl-2026-visualizer',GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ATTEMPT='1',REQUESTED_HEAD='test-commit',GITHUB_SHA='test-commit',REQUEST_SHA=handoff.sha(ROOT/'hosted_handoff/request.json'),RESEARCH_AUTOMATION_APPROVED='false',RESEARCH_PUBLISH_ENABLED='false',INITIAL_RESEARCH_RELEASE_ENABLED='false',GITHUB_RUN_ID='123',GH_TOKEN='unit-test-not-a-credential')
    def call_authority(self,env=None,r=None,responses=None):
        replies=iter(responses or [{'workflow_runs':[]}])
        def reply(*args,**kwargs):return io.BytesIO(json.dumps(next(replies)).encode())
        with patch.dict(os.environ,env or self.env(),clear=True),patch.object(handoff.urllib.request,'urlopen',side_effect=reply):handoff.authority(r or handoff.request())
    def test_valid_one_time_owner_authority(self):self.call_authority()
    def test_non_owner(self):
        e=self.env();e['GITHUB_ACTOR']='someone-else'
        with self.assertRaises(ValueError):self.call_authority(env=e)
    def test_expired_authority(self):
        r=handoff.request();r['expires_at_utc']=(handoff.now()-timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):self.call_authority(r=r)
    def test_rerun_is_not_another_grant(self):
        e=self.env();e['GITHUB_RUN_ATTEMPT']='2'
        with self.assertRaises(ValueError):self.call_authority(env=e)
    def test_prior_deployment_consumes_grant(self):
        replies=[{'workflow_runs':[{'id':122,'created_at':handoff.now().isoformat(),'jobs_url':'https://api.github.com/mock/jobs'}]},{'jobs':[{'steps':[{'name':'Deploy reviewed hosted site','conclusion':'success'}]}]}]
        with self.assertRaises(ValueError):self.call_authority(responses=replies)
    def test_failed_preflight_does_not_consume_publication(self):
        replies=[{'workflow_runs':[{'id':122,'created_at':handoff.now().isoformat(),'jobs_url':'https://api.github.com/mock/jobs'}]},{'jobs':[{'steps':[{'name':'Validate owner approval, data, current fixtures and last-known-good release','conclusion':'failure'}]}]}]
        self.call_authority(responses=replies)
    def test_manual_only_pinned_workflow_and_recovery(self):
        s=(ROOT/'.github/workflows/supervised-hosted-handoff.yml').read_text(encoding='utf-8')
        for bad in ['schedule:','pull_request:','pull_request_target:','secrets.','self-hosted']:self.assertNotIn(bad,s)
        self.assertIn('workflow_dispatch:',s);self.assertIn('github-pages-rollback',s);self.assertIn('needs.publish.result == \'failure\'',s)
        self.assertIn(handoff.sha(ROOT/'hosted_handoff/request.json'),s)
if __name__=='__main__':unittest.main()
