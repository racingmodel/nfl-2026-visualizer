"""Exact-scope research publication gate and propagation check; no self approval."""
from pathlib import Path
import argparse,os,sys,json,hmac,hashlib,urllib.request,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from nfl2026.weekly_research.core import load,sha,canonical,digest,immutable,clock,validate_publication

def propose(site,out,bundle):
    files={str(p.relative_to(site)).replace('\\','/'):sha(p) for p in site.rglob('*') if p.is_file() and str(p.relative_to(site)).replace('\\','/') not in {'data/weekly_research.json','data/weekly_research.sha256','release_receipt.json'}}
    policy=dict(schema='weekly-research-publication-policy-v1',season=2026,first_week=8,last_week=18,static_assets=files,model_hashes={'team':'3c31726c7ca9bea5a4683128b5b0befe0f7d4635608a1d6e7ac43af8b2268fec','player':'397b4353d921511f29252f28851578285ed6b6b82208a5c0bdc0da6ace171d3a'},bootstrap_manifest_sha256=sha(bundle/'bootstrap_manifest.json'),official_replacement=False,paid_requests=False,allowed_dynamic_files=['data/weekly_research.json','data/weekly_research.sha256','release_receipt.json'],allowed_updates=['validated_availability_context','fixed_model_consumed_feature_research_revisions','cutoff_locks','validated_final_results'],quarter_passing_TD_win_probabilities='WITHHELD',site='https://racingmodel.github.io/nfl-2026-visualizer/',approval_required=True,rollback_scope='Previously approved release with identical policy/static/model identities only')
    immutable(out,policy);immutable(out.with_name('approval.pending.json'),dict(decision='PENDING_HUMAN_APPROVAL',policy_sha256=digest(policy),initial_release_manifest_sha256=sha(site/'release_receipt.json'),publication_enabled=False));return policy
def verify(site,policy_path,approval_path,bundle,state):
    policy=load(policy_path);approval=load(approval_path);data=load(site/'data/weekly_research.json')
    if sha(site/'data/weekly_research.json')!=(site/'data/weekly_research.sha256').read_text(encoding='utf-8').strip():raise ValueError('Data asset mismatch')
    validate_publication(policy,approval,os.environ.get('RESEARCH_POLICY_HMAC_KEY',''),clock(),site,data)
    from scripts.weekly_forecasts import verify_bundle
    from nfl2026.weekly_research.core import Store
    verify_bundle(bundle)
    if sha(Path(bundle)/'bootstrap_manifest.json')!=policy['bootstrap_manifest_sha256']:raise ValueError('Publication bootstrap/runtime scope changed')
    periods={tuple(g['game_id'].split('_')[:2]) for g in data['games']}
    if len(periods)!=1:raise ValueError('Mixed publication weeks')
    season,week=next(iter(periods));store=Store(Path(state)/f'{season}-w{int(week):02d}');ref,selected=store.head()
    if ref!=data['commit'] or set(selected)!={g['game_id'] for g in data['games']}:raise ValueError('Publication does not match accepted state')
    for g in data['games']:
        if any(g.get(k)!=v for k,v in selected[g['game_id']].items()):raise ValueError('Published projection differs from accepted immutable revision')
    # Commit hashes and immutable audit originals must be bound in the public
    # projection, but private inputs/model weights never enter the site tree.
    if any('models/' in n or n.endswith(('.joblib','.pickle','.pkl','.tar.gz')) for n in policy['static_assets']):raise ValueError('Private model artifact in public assets')
    return dict(status='APPROVED_EXACT_RESEARCH_SCOPE',data_sha256=sha(site/'data/weekly_research.json'),policy_sha256=digest(policy),verified_at_utc=clock().isoformat())
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['propose','verify','propagation']);p.add_argument('--site',type=Path);p.add_argument('--policy',type=Path);p.add_argument('--approval',type=Path);p.add_argument('--bundle',type=Path);p.add_argument('--state',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    if a.command=='propose':print(json.dumps(propose(a.site,a.out,a.bundle)));return
    if a.command=='verify':r=verify(a.site,a.policy,a.approval,a.bundle,a.state)
    else:
        expected=sha(a.site/'data/weekly_research.json');url='https://racingmodel.github.io/nfl-2026-visualizer/data/weekly_research.json';actual=None;attempts=[]
        for i in range(4):
            try:
                b=urllib.request.urlopen(url+'?receipt='+expected[:16],timeout=20).read();actual=hashlib.sha256(b).hexdigest();attempts.append(dict(observed_at_utc=clock().isoformat(),sha256=actual))
                if actual==expected:break
            except Exception as exc:attempts.append(dict(observed_at_utc=clock().isoformat(),error=str(exc)))
            if i<3:time.sleep(15)
        r=dict(status='PROPAGATED_VERIFIED' if actual==expected else 'DEPLOYMENT_NOT_CONFIRMED_ALERT',expected_sha256=expected,attempts=attempts)
    immutable(a.out,r);print(json.dumps(r))
    if r['status']=='DEPLOYMENT_NOT_CONFIRMED_ALERT':raise SystemExit(1)
if __name__=='__main__':main()
