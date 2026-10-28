"""Portable scheduled controller. No publication calls or local-machine services."""
from pathlib import Path
import argparse,sys,json,shutil,tarfile,os
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from nfl2026.weekly_research.core import Store,clock,utc,load,immutable,canonical,digest,due,sha
from nfl2026.weekly_research.runtime import capture,FixedInference
from scripts.weekly_forecasts import verify_bundle,import_seed,export,restore_initial

def retain_sources(captured,state,packet):
    """Content-addressed raw bytes survive temporary execution artifacts."""
    blobs=Path(state)/'sources/blobs';blobs.mkdir(parents=True,exist_ok=True);refs={}
    for p in Path(captured).rglob('*'):
        if p.is_file():
            h=sha(p);dest=blobs/h
            if not dest.exists():shutil.copyfile(p,dest)
            if sha(dest)!=h:raise ValueError('Durable raw source drift')
            refs[str(p.relative_to(captured)).replace('\\','/')]=h
    immutable(Path(state)/'sources/snapshots'/(digest(packet)+'.json'),dict(packet_sha256=digest(packet),captured_files=refs))

def attach_prepared_players(bundle,state,captured,packet,current=None):
    """Retry missing next-week data on later polls, never invent a feature block."""
    from nfl2026.weekly_research.runtime import prepare_next_players
    import pandas as pd
    from nfl2026.features.player_advanced import PLAYER_FEATURES
    path,receipt=prepare_next_players(bundle,state,captured,packet['season'],packet['week']);frame=pd.read_csv(path,encoding='utf-8',low_memory=False,float_precision='round_trip');roster=pd.read_csv(Path(captured)/'normalised/roster.csv',encoding='utf-8',low_memory=False);eligible=set(roster[roster.status.eq('ACT')].gsis_id.astype(str));previous={p['player_id'] for g in (current or {}).values() for p in g['players']};frame=frame[frame.player_id.astype(str).isin(eligible|previous)];fields=['game_id','player_id','player_name','team','opponent','position','kickoff_utc',*PLAYER_FEATURES]
    packet['player_input_provenance']={}
    for gid,part in frame.groupby('game_id'):
        packet['player_inputs'][gid]=part[fields].astype(object).where(part[fields].notna(),None).to_dict('records');packet['player_input_provenance'][gid]=receipt
    return receipt

def safe_unpack(archive,out,expected_sha):
    if sha(archive)!=expected_sha:raise ValueError('Private bootstrap archive hash differs')
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    with tarfile.open(archive,'r:gz') as tf:
        for m in tf.getmembers():
            if not (out/m.name).resolve().is_relative_to(out) or not (m.isfile() or m.isdir()):raise ValueError('Unsafe archive path/link')
        tf.extractall(out,filter='data')
    verify_bundle(out)

def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--state',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--manual',action='store_true');p.add_argument('--first-bootstrap',action='store_true');p.add_argument('--week',type=int,default=8);a=p.parse_args();verify_bundle(a.bundle);a.out.mkdir(parents=True,exist_ok=False)
    periods=sorted(a.state.glob('2026-w*/HEAD.json'));active=load(a.state/'active_period.json') if (a.state/'active_period.json').exists() else dict(season=2026,week=a.week);a.week=int(active['week']);store=Store(a.state/f'2026-w{a.week:02d}');ref,current=store.head()
    if not ref:
        if not a.first_bootstrap:raise ValueError('Prior accepted cloud state missing: explicit bootstrap/restore required; never reset originals silently')
        if a.week!=8:raise ValueError('A new-week seed requires fresh source generation, not Week8 replay')
        restore_initial(store,a.bundle)
    last=None;receipts=sorted((store.root/'receipts').glob('*.json'))
    for r in reversed(receipts):
        v=load(r)
        if v.get('observed_at_utc') and v.get('status') in {'UPDATED','UNCHANGED'}:last=v['observed_at_utc'];break
    _,current=store.head();yes,why=due(clock(),list(current.values()),last)
    failed=[]
    for pth in reversed(receipts):
        record=load(pth)
        if record.get('status') in {'UPDATED','UNCHANGED'}:break
        if record.get('status') in {'FAILED_LAST_KNOWN_GOOD_RETAINED','CLOUD_EXECUTION_FAILED'}:failed.append(record)
    if failed:
        from datetime import timedelta
        # Inner inference failure and outer job alert describe ONE actual
        # execution. Manual reruns are distinct GitHub run attempts.
        unique={}
        for row in failed:
            key=(row.get('github_run_id'),row.get('github_run_attempt')) if row.get('github_run_id') else row['actual_receipt_written_at_utc']
            unique.setdefault(key,row)
        failed=list(unique.values())
        near=any(utc(g['cutoff_utc'])-timedelta(hours=2)<=clock()<utc(g['cutoff_utc']) for g in current.values());limit=9 if near else 3
        if len(failed)<limit and clock()-utc(failed[0]['actual_receipt_written_at_utc'])>=timedelta(minutes=15):yes,why=True,'BOUNDED_FAILURE_RETRY'
        elif len(failed)>=limit and why not in {'UK_SCHEDULE_SLOT','CUTOFF_CLOSURE_DUE','MISSED_UK_SLOT_CATCHUP'}:yes,why=False,'RETRY_LIMIT_REACHED_ALERT'
    source_packets=sorted((store.root/'source_packets').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    if source_packets and last:
        if any(utc(last)<utc(t)<=clock() for t in load(source_packets[0]).get('next_feature_events_utc',[])):yes,why=True,'CONTRACT_FEATURE_EMBARGO_EXPIRED'
    if not a.manual and not yes:immutable(a.out/'execution_receipt.json',dict(status='NOT_DUE_NO_NETWORK',actual_at_utc=clock().isoformat(),reason=why));return
    packet=capture(a.out/'capture',a.bundle,2026,a.week)
    if a.week>8 and any(not g['players'] for g in current.values()):
        try:immutable(a.out/'player_preparation_retry.json',attach_prepared_players(a.bundle,a.state,a.out/'capture',packet,current))
        except Exception as exc:immutable(a.out/'player_preparation_retry.json',dict(status='PLAYER_INPUTS_STILL_PENDING_UNKNOWN',error=str(exc)))
    retain_sources(a.out/'capture',a.state,packet);result=store.refresh(packet,clock(),FixedInference(a.bundle));_,latest=store.head()
    if all(g['state']=='FINAL' for g in latest.values()) and a.week<18:
        next_week=a.week+1;next_packet=capture(a.out/'next_capture',a.bundle,2026,next_week);retain_sources(a.out/'next_capture',a.state,next_packet);new_store=Store(a.state/f'2026-w{next_week:02d}')
        if not new_store.head()[0]:new_store.initialise(dict(games=[dict(g,away_expected_points=None,home_expected_points=None,model_margin=None,model_total=None,expected_QBs=[]) for g in next_packet['games']],players=[]),clock(),digest(next_packet))
        try:
            receipt=attach_prepared_players(a.bundle,a.state,a.out/'next_capture',next_packet)
            immutable(a.out/'player_preparation_receipt.json',receipt)
        except Exception as exc:immutable(a.out/'player_preparation_receipt.json',dict(status='WITHHELD_UNSUPPORTED_PLAYER_INPUTS_TEAM_FORECASTS_INDEPENDENT',error=str(exc)))
        result=new_store.refresh(next_packet,clock(),FixedInference(a.bundle));store=new_store
        from nfl2026.weekly_research.core import atomic
        atomic(a.state/'active_period.json',canonical(dict(season=2026,week=next_week,transition_at_utc=clock().isoformat(),prior_all_FINAL=True)))
    export(store,a.bundle,a.out/'site');immutable(a.out/'execution_receipt.json',dict(result,reason=why,actual_completed_at_utc=clock().isoformat(),local_machine_dependency=False,cloud_execution_not_claimed_without_GitHub_run_id=True,github_run_id=os.environ.get('GITHUB_RUN_ID'),publication=False))
    _,latest=store.head()
    if all(g['state']=='FINAL' for g in latest.values()):
        immutable(a.out/'next_week_readiness.json',dict(status='PREVIOUS_WEEK_FINAL_PREPARE_NEXT',next_week=a.week+1,player_feature_preparation='Use existing isolated prepare_weekly_prediction/rollforward only with authenticated canonical stats/PBP and exact next-week roster; unsupported features remain withheld',automatic_model_training=False))
    print(json.dumps(result),flush=True)
if __name__=='__main__':main()
