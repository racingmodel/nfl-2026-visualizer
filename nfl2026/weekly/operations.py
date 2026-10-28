"""Observable checks; a configured or scheduled task is not execution evidence."""
from datetime import datetime,timezone,timedelta
import pandas as pd

QB_FIELDS=[f'{side}_{name}' for side in ['away','home'] for name in ['expected_qb_id','expected_qb_name','qb_source_reliability']]
def source_gate(row,*,season,week,now,max_age_hours):
    errors=[];observed=pd.to_datetime(row.get('observed_at_utc'),utc=True,errors='coerce');clock=pd.Timestamp(now)
    if row.get('season')!=season or row.get('week')!=week:errors.append('WRONG_SEASON_WEEK')
    if pd.isna(observed) or observed>clock:errors.append('OBSERVATION_CLOCK_INVALID')
    elif (clock-observed).total_seconds()>max_age_hours*3600:errors.append('SOURCE_STALE')
    publication=pd.to_datetime(row.get('published_at_utc'),utc=True,errors='coerce');modified=pd.to_datetime(row.get('modified_at_utc'),utc=True,errors='coerce')
    if pd.notna(publication) and pd.notna(modified) and modified<publication:errors.append('MODIFIED_BEFORE_PUBLISHED')
    if any(pd.notna(c) and (pd.isna(observed) or c>observed) for c in [publication,modified]):errors.append('SOURCE_CLOCK_AFTER_OBSERVATION')
    if row.get('complete_final') is not True:errors.append('INCOMPLETE_FINAL_DESIGNATIONS')
    return dict(status='PASS' if not errors else 'BLOCKED',issues=errors)

def qb_archive_schema(columns):
    missing=sorted(set(QB_FIELDS)-set(columns));return dict(status='PASS' if not missing else 'BLOCKED',missing_fields=missing)

def checkpoint_state(checkpoint,receipts,now):
    now=pd.Timestamp(now);deadline=pd.Timestamp(checkpoint['deadline_utc']);due=pd.Timestamp(checkpoint['due_utc']);seen=set();real=[];missed=[];future=0
    for r in receipts:
        if r.get('checkpoint_id')!=checkpoint['id']:continue
        start=pd.to_datetime(r.get('started_at_utc'),utc=True,errors='coerce')
        if pd.isna(start):continue
        if start>now:future+=1;continue
        key=(r['checkpoint_id'],start.isoformat())
        if key in seen:continue
        seen.add(key)
        if r.get('status')=='MISSED_CHECKPOINT_NO_PREGAME_BACKDATING' or start>=deadline:missed.append(r);continue
        if start<due:continue
        real.append(r)
    cap=[r for r in real if int(r.get('network_requests',0))>0]
    complete=False
    for r in cap:
        finished=pd.to_datetime(r.get('reconciliation_completed_at_utc',r.get('completed_at_utc')),utc=True,errors='coerce')
        digest=r.get('certification_manifest_sha256','')
        if r.get('status')=='RECONCILED_CERTIFIED' and pd.notna(finished) and pd.Timestamp(r['started_at_utc'])<=finished<deadline and finished<=now and len(digest)==64 and all(c in '0123456789abcdef' for c in digest):complete=True
    if now>=deadline:status='CLOSED_COMPLETE' if complete else 'CLOSED_PENDING_OR_MISSED';next_time=None
    elif len(real)>=checkpoint['max_attempts']:status='ATTEMPT_LIMIT';next_time=None
    elif now<due:status='NOT_DUE';next_time=due.isoformat()
    else:
        candidate=now+pd.Timedelta(minutes=checkpoint['retry_minutes']);next_time=candidate.isoformat() if candidate<deadline else None;status='PENDING_RECONCILIATION' if cap else 'MISSING_EXECUTION'
    return dict(checkpoint_id=checkpoint['id'],status=status,actual_attempts=len(real),actual_network_captures=len(cap),late_fail_closed_invocations=len(missed),
        complete_certification_proven=complete,future_receipts_rejected=future,next_retry_utc=next_time,deadline_utc=deadline.isoformat(),scheduling_is_execution_proof=False)

def publication_check(expected,observed):
    """Expected means an explicitly deployed identity, never an unpublished candidate."""
    issues=[]
    for name,body in expected.items():
        if name not in observed:issues.append(dict(asset=name,state='UNOBSERVED'))
        elif body.replace(b'\r\n',b'\n')!=observed[name].replace(b'\r\n',b'\n'):issues.append(dict(asset=name,state='PROPAGATION_MISMATCH'))
    return dict(status='VERIFIED_OBSERVED_ASSETS' if not issues else 'INCOMPLETE_OR_MISMATCHED',assets=len(expected),issues=issues,publication_performed=False)
