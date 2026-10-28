"""Portable initial/refresh/manual/cloud entry points; publication separate."""
from pathlib import Path
import sys,argparse,shutil,json,tarfile,os
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from nfl2026.weekly_research.core import Store,clock,canonical,sha,load,digest,immutable,due,utc,validate_publication
from nfl2026.weekly_research.runtime import capture,FixedInference,values

def import_seed(bundle):
    import pandas as pd
    b=Path(bundle);seed=load(b/'seed/week8.json');indices=load(b/'seed/original_input_hashes.json')
    for g in seed['games']:g['team_input_hash']=indices.get(g['game_id'])
    return seed

def bootstrap(out,seed_store=None):
    import pandas as pd
    out=Path(out);out.mkdir(parents=True,exist_ok=False);phase=ROOT/'reports/week8_phase14_20261028T110437Z';(out/'models').mkdir();(out/'seed').mkdir();(out/'config').mkdir();(out/'ui').mkdir()
    shutil.copyfile(phase/'week8_run/causal_model.joblib',out/'models/team.joblib');shutil.copyfile(ROOT/'artifacts/models/player-phase2-v3-rush-et.joblib',out/'models/player.joblib')
    for p in (ROOT/'config').glob('*'):
        if p.name in ['nfl2026.toml','availability_sources.json','qb_evidence_sources.json','transaction_evidence_sources.json','official_roster_sources.json']:shutil.copyfile(p,out/'config'/p.name)
    # Exact V3.2 reuse. Only NEW candidate root index changes, originals preserved.
    shutil.copytree(phase/'candidate-v4',out/'ui',dirs_exist_ok=True);shutil.copyfile(out/'ui/index.html',out/'ui/historical-index.html')
    shutil.copyfile(phase/'week8_integrated_research_candidate-v4.json',out/'seed/week8.json');indices={};sources={};replay=[]
    source_blobs={}
    for p in (ROOT/'artifacts/research/week8_prospective_issuances/2026/week_08').glob('*.json'):
        issue=load(p);f=ROOT/issue['freeze_path'];assert sha(f)==issue['freeze_sha256'];freeze=load(f);snap=ROOT/freeze['source_snapshot'];frame=pd.read_csv(snap/'team_inputs.csv',float_precision='round_trip',encoding='utf-8')
        match=frame[frame.game_id.eq(issue['game_id'])]
        if len(match)!=1:raise ValueError('Original first inputs unresolved')
        indices[issue['game_id']]=digest(values(match.iloc[0].to_dict()));sources[issue['game_id']]=dict(freeze_id=issue['freeze_id'],freeze_sha256=issue['freeze_sha256']);expected=pd.read_csv(f.parent/'shadow_predictions.csv',float_precision='round_trip',encoding='utf-8');expected=expected[expected.game_id.eq(issue['game_id'])].iloc[0];replay.append(dict(game_id=issue['game_id'],**values(match.iloc[0].to_dict()),expected_home=float(expected.home_expected_points),expected_away=float(expected.away_expected_points)))
        # Original freeze bytes and all input/output bytes travel privately;
        # original path strings are not rewritten to manufacture new provenance.
        for label,folder,files in [('inputs',snap,freeze['source_snapshot_hashes']),('outputs',f.parent,freeze['output_files']),('freeze',f.parent,{f.name:sha(f)})]:
            for n,h in files.items():
                if sha(folder/n)!=h:raise ValueError('Original first freeze drift')
                dest=out/'seed/original_blobs'/h;dest.parent.mkdir(parents=True,exist_ok=True)
                if not dest.exists():shutil.copyfile(folder/n,dest)
                source_blobs[freeze['freeze_id']+'/'+label+'/'+n]=h
    immutable(out/'seed/original_input_hashes.json',indices);immutable(out/'seed/original_freeze_lineage.json',sources)
    immutable(out/'seed/team_replay.json',replay)
    immutable(out/'seed/original_blob_lineage.json',source_blobs)
    if seed_store:
        seed_store=Path(seed_store);Store(seed_store).head();shutil.copytree(seed_store,out/'seed/initial_state')
    shutil.move(out/'ui/release_manifest.json',out/'ui/seed_release_manifest.json')
    for name in ['index.html','weekly.js','weekly.css']:shutil.copyfile(ROOT/'frontend/weekly_research_src'/name,out/'ui'/name)
    # Portable prior-state resources for the EXISTING player rollforward only.
    resources=['artifacts/canonical/player_weekly.csv','artifacts/canonical/player_pbp_weekly.csv','artifacts/canonical/canonical_games.csv','artifacts/phase2/player_pregame_universe_features.csv','artifacts/phase2/player_advanced_conditional_matrix.csv','artifacts/phase2/pregame_player_universe.csv']
    for name in resources:
        p=ROOT/name
        if p.is_file():q=out/'preparation'/name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
    # Only target week’s prepared inputs are eligible until new manifest validation.
    source=ROOT/'artifacts/phase2/player_pregame_universe_features.csv';p=pd.read_csv(source,encoding='utf-8',low_memory=False);p[p.season.eq(2026)&p.week.eq(8)].to_csv(out/'seed/player_features_w08.csv',index=False)
    impl=['nfl2026','scripts/weekly_forecasts.py','scripts/weekly_cloud.py','scripts/weekly_cloud_state.py','scripts/weekly_release.py']
    for name in impl:
        src=ROOT/name;dst=out/'runtime'/name
        if src.is_dir():shutil.copytree(src,dst,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        else:dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
    immutable(out/'bootstrap_manifest.json',dict(schema='weekly-research-bootstrap-v1',created_at_utc=clock().isoformat(),private_model_bundle_not_for_Pages=True,files={str(p.relative_to(out)).replace('\\','/'):sha(p) for p in out.rglob('*') if p.is_file()}))
    print(out,flush=True)

def verify_bundle(b):
    b=Path(b);m=load(b/'bootstrap_manifest.json')
    for n,h in m['files'].items():
        p=(b/n).resolve()
        if not p.is_relative_to(b.resolve()) or not p.is_file() or sha(p)!=h:raise ValueError('Bootstrap identity drift:'+n)
        if n.startswith('runtime/'):
            live=ROOT/n.removeprefix('runtime/')
            if not live.is_file() or sha(live)!=h:raise ValueError('Running implementation differs from reviewed portable bundle:'+n)
    return m

def restore_initial(store,bundle):
    source=Path(bundle)/'seed/initial_state'
    if store.head()[0]:raise ValueError('Existing accepted state; initial reset forbidden')
    if source.is_dir():
        shutil.copytree(source,store.root,dirs_exist_ok=True);store.head();return store.receipt(dict(status='INITIAL_WEEK_FORECAST_RESTORED_EXACT_ACCEPTED_STATE',originals_not_reinferred=True,bootstrap_manifest_sha256=sha(Path(bundle)/'bootstrap_manifest.json')))
    return store.initialise(import_seed(bundle),clock(),sha(Path(bundle)/'seed/week8.json'))

def export(store,bundle,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False);shutil.copytree(Path(bundle)/'ui',out,dirs_exist_ok=True);data=store.view(clock());b=canonical(data);(out/'data/weekly_research.json').write_bytes(b);(out/'data/weekly_research.sha256').write_text(sha(out/'data/weekly_research.json'),encoding='utf-8')
    files={str(p.relative_to(out)).replace('\\','/'):sha(p) for p in out.rglob('*') if p.is_file()};immutable(out/'release_receipt.json',dict(schema='weekly-local-release-candidate-v1',release_id='weekly-research-'+digest(files)[:16],created_at_utc=clock().isoformat(),data_sha256=sha(out/'data/weekly_research.json'),store_commit=data['commit'],official=False,publication=False,files=files));return data

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['bootstrap','initial','refresh','manual','export','check']);p.add_argument('--bundle',type=Path);p.add_argument('--store',type=Path);p.add_argument('--out',type=Path);p.add_argument('--capture',type=Path);p.add_argument('--seed-store',type=Path);p.add_argument('--season',type=int,default=2026);p.add_argument('--week',type=int,default=8);p.add_argument('--packet',type=Path);a=p.parse_args()
    if a.command=='bootstrap':return bootstrap(a.out,a.seed_store)
    verify_bundle(a.bundle);store=Store(a.store)
    if a.command=='initial':
        if a.week==8 and a.season==2026:print(json.dumps(restore_initial(store,a.bundle)),flush=True)
        else:
            if not a.capture:raise ValueError('New week requires a new actual free-source capture directory')
            packet=capture(a.capture,a.bundle,a.season,a.week);seed=dict(games=[dict(g,away_expected_points=None,home_expected_points=None,model_margin=None,model_total=None,expected_QBs=[]) for g in packet['games']],players=[]);store.initialise(seed,clock(),digest(packet));print(json.dumps(store.refresh(packet,clock(),FixedInference(a.bundle),mode='INITIAL_WEEK_FORECAST')),flush=True)
    elif a.command in {'refresh','manual'}:
        packet=load(a.packet) if a.packet else capture(a.capture,a.bundle,a.season,a.week)
        result=store.refresh(packet,clock(),FixedInference(a.bundle));print(json.dumps(result),flush=True)
    elif a.command=='export':export(store,a.bundle,a.out);print(a.out,flush=True)
    else:print(json.dumps(store.head()[0]),flush=True)
if __name__=='__main__':main()
