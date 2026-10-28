"""One cutoff-aware transaction state machine for roster and production gates."""
from __future__ import annotations
import json
import re
import pandas as pd

INACTIVE={'IR','RESERVE','PUP','INACTIVE','RELEASED','WAIVED','PRACTICE_SQUAD','SUSPENDED','CONFLICT'}

def key(value):return re.sub(r'[^a-z0-9]','',str(value).lower())
def clean(value):return '' if pd.isna(value) or str(value).lower() in {'nan','none'} else str(value)

def classify(description):
    t=str(description).lower()
    if 'suspension lifted' in t:return 'SUSPENSION_LIFTED'
    if any(x in t for x in ('exempt/commissioner permission','contract extension')):return 'NO_ROSTER_CHANGE'
    if any(x in t for x in ('designated for return','return designated','return to practice')):return 'RETURN_DESIGNATED'
    if 'reverted' in t and 'practice' in t:return 'PRACTICE_SQUAD'
    if any(x in t for x in ('activated','reinstated','returned from reserve','returned to active')):return 'ACTIVE'
    if 'reserve/suspended' in t or 'suspended' in t:return 'SUSPENDED'
    if any(x in t for x in ('injured reserve','reserve/injured','reserve/pup','reserve/suspended','placed on injured list')):return 'IR'
    if any(x in t for x in ('waived','released','terminated')):return 'RELEASED'
    if 'elevat' in t:return 'ELEVATED'
    if 'practice squad' in t and ('to practice' in t or 'to the practice' in t or 'practice squad veteran' in t):return 'PRACTICE_SQUAD'
    if 'practice squad' in t and 'from' not in t and not any(x in t for x in ('active roster','53-man','promot')):return 'PRACTICE_SQUAD'
    if any(x in t for x in ('signed','signing','promot','free agent','active roster','53-man')):return 'ACTIVE'
    if 'reserve' in t:return 'IR'
    return 'UNKNOWN'

def matching_events(events, player_id, player_name, team, pfr_id=''):
    rows=[]
    for row in events.to_dict('records'):
        pid,pfr=clean(row.get('player_id','')),clean(row.get('pfr_id',''))
        if pid:
            match=pid==str(player_id)
            if match and clean(row.get('player_name','')) and key(row['player_name'])!=key(player_name):
                raise ValueError('Transaction player ID/name identity mismatch')
        elif pfr and pfr_id:
            match=pfr==pfr_id
        else:
            match=(key(row.get('player_name',''))==key(player_name) and str(team) in
                   {clean(row.get('from_team','')),clean(row.get('to_team',''))})
        if match:
            rows.append(row)
    return rows

def resolve_player_state(events, player_id, player_name, team, *, cutoff=None, pfr_id='', roster_known_at=None):
    rows=matching_events(events,player_id,player_name,team,pfr_id)
    if not rows:return None
    limit=pd.Timestamp(cutoff) if cutoff is not None else None
    if limit is not None and limit.tzinfo is None:raise ValueError('Transaction cutoff requires timezone')
    usable=[]
    for row in rows:
        when=pd.to_datetime(row.get('event_at_utc'),utc=True,errors='coerce')
        if pd.isna(when):raise ValueError('Transaction eligibility evidence has unknown event time')
        known=pd.to_datetime(row.get('known_at_utc'),utc=True,errors='coerce')
        observed=pd.to_datetime(row.get('retrieved_at_utc'),utc=True,errors='coerce')
        if limit is not None:
            if pd.isna(known) or pd.isna(observed):raise ValueError('Transaction publication/retrieval evidence missing')
            if known>limit or observed>limit or when>limit:continue
        if clean(row.get('source_state','VALID'))!='VALID':raise ValueError('Transaction source state is not VALID')
        state=clean(row.get('transaction_state','UNKNOWN')).upper()
        if state not in INACTIVE|{'ACTIVE','TEAM_CHANGE','RETURN_DESIGNATED','ELEVATED','NO_ROSTER_CHANGE','SUSPENSION_LIFTED'}:
            raise ValueError('Unrecognized transaction for '+str(player_name)+': '+str(row.get('transaction_detail','')))
        row={**row,'_when':when,'transaction_state':state}
        usable.append(row)
    if not usable:return None
    # Cross-source duplicates remain available as provenance, but cannot alter state.
    state=None;member=str(team);last=None;temporary=[];ambiguous=False
    for day in sorted({r['_when'].date() for r in usable}):
        block=[r for r in usable if r['_when'].date()==day]
        transitions=[r for r in block if r['transaction_state'] not in {'RETURN_DESIGNATED','ELEVATED','NO_ROSTER_CHANGE'}]
        temporary.extend(r for r in block if r['transaction_state']=='ELEVATED')
        if not transitions:continue  # Return-designation/elevation is not permanent activation.
        states={r['transaction_state'] for r in transitions}
        precision={clean(r.get('effective_precision','EXACT')) for r in transitions}
        if len(states)>1 and 'DATE' in precision:
            # A club's ordered statements within one dated table cell can
            # disambiguate a sequence only if they account for every state.
            groups={clean(r.get('sequence_group','')) for r in transitions}-{''}
            chains=[]
            for group in groups:
                chain=[r for r in transitions if clean(r.get('sequence_group',''))==group]
                if states.issubset({r['transaction_state'] for r in chain}):
                    chains.append(sorted(chain,key=lambda r:int(r['sequence_index']))[-1])
            if chains and len({(r['transaction_state'],clean(r.get('to_team','')) or clean(r.get('from_team',''))) for r in chains})==1:
                chosen=chains[-1]
            else:
                chosen=transitions[-1];state='CONFLICT';last=chosen;ambiguous=True;continue
        else:
            priority=lambda r:int(r['transaction_state'] in INACTIVE)
            chosen=sorted(transitions,key=lambda r:(r['_when'],priority(r),clean(r.get('event_id',''))))[-1]
            latest=[r for r in transitions if r['_when']==chosen['_when'] and r['transaction_state'] in {'ACTIVE','TEAM_CHANGE'}]
            teams={clean(r.get('to_team','')) or clean(r.get('from_team','')) for r in latest}-{''}
            if len(teams)>1:raise ValueError('Conflicting official transaction team assignments')
        new_member=clean(chosen.get('to_team','')) or clean(chosen.get('from_team',''))
        if not new_member:raise ValueError('Transaction has no identified team')
        state=chosen['transaction_state'];member=new_member;last=chosen;ambiguous=False
    if state is None:return None
    reference=pd.to_datetime(roster_known_at,utc=True,errors='coerce')
    if state=='TEAM_CHANGE' and member!=str(team) and pd.notna(reference):
        known=pd.to_datetime(last.get('known_at_utc'),utc=True,errors='coerce')
        if pd.notna(known) and known<reference and last['_when']<reference:
            return None  # Later exact membership snapshot supersedes an old trade.
    evidence=[{k:v for k,v in r.items() if not k.startswith('_')} for r in usable]
    return {'state':state,'team':member,'event':{k:v for k,v in last.items() if not k.startswith('_')},
            'evidence':evidence,'temporary_elevations':temporary,'ambiguous_order':ambiguous}

def apply_states(roster, events, *, cutoff=None, roster_known_at=None):
    out=roster.copy()
    out['roster_source_known_at_utc']=str(roster_known_at) if roster_known_at is not None else ''
    for c in ('current_transaction_state','current_transaction_event_at_utc','current_transaction_source',
              'current_transaction_evidence','transaction_reconciliation_error'):
        out[c]=''
    counts={'records_considered':0,'matched':0,'unmatched_player_status':0,'team_changes':0}
    if events.empty:return out,counts
    # Name-only evidence must identify a unique roster player across teams.
    if 'full_name' in out:
        names=out.assign(_name=out.full_name.map(key)).groupby('_name').gsis_id.nunique()
        for n in events.player_name.fillna('').map(key).unique():
            if n and names.get(n,0)>1:raise ValueError('Ambiguous name-only transaction player identity')
    for idx,row in out.iterrows():
        if 'full_name' not in row:continue
        resolved=resolve_player_state(events,str(row.gsis_id),str(row.full_name),str(row.team),
            cutoff=cutoff,pfr_id=clean(row.get('pfr_id','')),
            roster_known_at=roster_known_at if int(row.get('roster_source_week',row.week))==int(row.week) else None)
        if not resolved:continue
        event=resolved['event'];state=resolved['state']
        out.loc[idx,'current_transaction_state']=state
        out.loc[idx,'current_transaction_event_at_utc']=str(event['event_at_utc'])
        out.loc[idx,'current_transaction_source']=clean(event.get('source',''))
        out.loc[idx,'current_transaction_evidence']=json.dumps(resolved['evidence'],default=str,separators=(',',':'))
        counts['matched']+=1;counts['records_considered']+=len(resolved['evidence'])
        if resolved['team']!=str(row.team):
            exact=int(row.get('roster_source_week',row.week))==int(row.week)
            reference=pd.to_datetime(roster_known_at,utc=True,errors='coerce')
            when=pd.to_datetime(event['event_at_utc'],utc=True)
            if exact and pd.notna(reference) and when<=reference:
                # A dated current roster on another team is a new membership
                # observation; do not undo it with a months-old trade event.
                out.loc[idx,'current_transaction_state']=''
                continue
            if state not in {'ACTIVE','TEAM_CHANGE'}:
                out.loc[idx,'transaction_reconciliation_error']='Roster membership conflicts with latest transaction team'
            else:
                out.loc[idx,'team']=resolved['team'];counts['team_changes']+=1
        # A prior transaction does not silently overwrite a contradictory newer
        # exact roster snapshot. Keep it visible to the final production gate.
        exact=int(row.get('roster_source_week',row.week))==int(row.week)
        when=pd.Timestamp(event['event_at_utc'])
        when=when.tz_localize('UTC') if when.tzinfo is None else when
        reference=pd.to_datetime(roster_known_at,utc=True,errors='coerce')
        newer=pd.notna(reference) and when>reference
        current_active=str(row.status).upper()=='ACT'
        if state=='CONFLICT':
            out.loc[idx,'transaction_reconciliation_error']='Unordered conflicting same-day official transactions'
        elif not exact or newer or (current_active and state in {'ACTIVE','TEAM_CHANGE'}) or (not current_active and state in INACTIVE):
            out.loc[idx,'status']='ACT' if state in {'ACTIVE','TEAM_CHANGE'} else state
        elif current_active and state in INACTIVE:
            out.loc[idx,'transaction_reconciliation_error']='Active roster conflicts with latest inactive transaction'
    matched_keys=set(out.get('full_name',pd.Series(dtype=str)).map(key))
    counts['unmatched_player_status']=sum(key(r.get('player_name','')) not in matched_keys for r in events.to_dict('records') if r.get('transaction_state') in INACTIVE)
    return out,counts
