"""Team/position snapshot selection with explicit, bounded fallback evidence."""
import json
import pandas as pd
from nfl2026.live.providers.transaction_state import resolve_player_state,INACTIVE

MAX_DEPTH_AGE_HOURS=36  # Existing QB depth freshness boundary, now row/position-specific.
OFFENSE={'QB','RB','FB','WR','TE'}


def select_position_snapshots(raw,teams,cutoff,*,roster=None,injuries=None,transactions=None,transaction_coverage=None,max_age_hours=MAX_DEPTH_AGE_HOURS):
    stop=pd.Timestamp(cutoff)
    if stop.tzinfo is None:raise ValueError('Depth cutoff requires timezone')
    d=raw.copy();d['dt']=pd.to_datetime(d.dt,utc=True,errors='coerce');d=d[d.dt.le(stop)&d.team.isin(teams)]
    selected=[];audit=[]
    for team,t in d.groupby('team'):
        latest=t.dt.max()
        positions=set(t.pos_abb.dropna())|{'QB','RB','WR','TE'}
        for position in sorted(positions):
            group=t[t.pos_abb.eq(position)];when=group.dt.max();rows=group[group.dt.eq(when)].copy()
            age=(stop-when).total_seconds()/3600 if pd.notna(when) else None
            older=pd.notna(when) and when<latest;reason='';accepted=rows.copy()
            # Rank within the original slot before availability exclusions. A
            # removed starter can never promote its backup by re-ranking.
            rankcols=['team','pos_abb','pos_grp_id','pos_id','pos_slot']
            if len(rows) and set(rankcols).issubset(rows):
                rows['snapshot_depth_order']=rows.groupby(rankcols,dropna=False).pos_rank.rank(method='first').astype('Int64')
            elif len(rows):rows['snapshot_depth_order']=pd.to_numeric(rows.pos_rank,errors='coerce').astype('Int64')
            accepted=rows.copy();excluded=[]
            if age is None:reason='POSITION_MISSING'
            elif age>max_age_hours:reason='POSITION_STALE'
            elif older and position in OFFENSE:
                if roster is None or injuries is None or transactions is None or transaction_coverage is None:
                    reason='PRIOR_POSITION_REQUIRES_ROSTER_INJURY_TRANSACTION_RECONCILIATION'
                elif transaction_coverage.empty or 'coverage_status' not in transaction_coverage or not transaction_coverage.coverage_status.eq('OFFICIAL_CURRENT_MONTH').all():reason='TRANSACTION_COVERAGE_INCOMPLETE'
                elif not {'known_at_utc','retrieved_at_utc'}.issubset(transaction_coverage) or any(pd.to_datetime(transaction_coverage[c],utc=True,errors='coerce').isna().any() or pd.to_datetime(transaction_coverage[c],utc=True).gt(stop).any() for c in ['known_at_utc','retrieved_at_utc']):reason='TRANSACTION_COVERAGE_TIME_UNPROVED'
                else:
                    coverage=injuries[injuries.record_type.eq('TEAM_COVERAGE')&injuries.team.eq(team)]
                    if coverage.empty or not coverage.source_state.eq('VALID').all():reason='INJURY_COVERAGE_UNRESOLVED'
                    elif pd.to_datetime(coverage.known_at_utc,utc=True,errors='coerce').isna().any() or pd.to_datetime(coverage.known_at_utc,utc=True).gt(stop).any():reason='INJURY_COVERAGE_NOT_KNOWN'
                    else:
                        keep=[]
                        for idx,r in rows.iterrows():
                            member=roster[roster.gsis_id.eq(r.gsis_id)&roster.team.eq(team)]
                            why=''
                            if member.empty or member.gsis_id.duplicated().any():why='ROSTER_IDENTITY_MISSING_OR_DUPLICATE'
                            else:
                                m=member.iloc[-1];known=pd.to_datetime(m.get('roster_source_known_at_utc'),utc=True,errors='coerce')
                                if pd.isna(known) or known>stop:why='ROSTER_TIME_UNPROVED'
                                elif str(m.get('position','')).upper()!=str(position).upper():why='ROSTER_POSITION_CONFLICT'
                                elif str(m.status)!='ACT' or str(m.get('current_transaction_state','')).upper() in INACTIVE:why='ROSTER_OR_TRANSACTION_UNAVAILABLE'
                                elif str(m.get('transaction_reconciliation_error','') or '').strip() not in {'','nan'}:why='ROSTER_TRANSACTION_CONFLICT'
                                else:
                                    state=resolve_player_state(transactions,str(r.gsis_id),str(r.player_name),team,cutoff=stop,roster_known_at=known)
                                    if state and (state['state'] in INACTIVE or state['team']!=team):why='LATER_TRANSACTION_SUPERSEDES_DEPTH'
                                    health=injuries[injuries.record_type.eq('PLAYER_REPORT')&injuries.player_id.eq(r.gsis_id)&injuries.team.eq(team)].copy()
                                    if len(health):health=health[pd.to_datetime(health.known_at_utc,utc=True,errors='coerce').le(stop)]
                                    if len(health) and health.game_status.astype(str).str.upper().isin(INACTIVE|{'OUT','QUESTIONABLE','DOUBTFUL'}).any():why='INJURY_AVAILABILITY_UNRESOLVED_OR_UNAVAILABLE'
                            if why:excluded.append({'player_id':str(r.gsis_id),'reason':why})
                            else:keep.append(idx)
                        accepted=rows.loc[keep]
            if reason:accepted=rows.iloc[:0]
            if len(accepted):
                accepted=accepted.assign(snapshot_selection='VERIFIED_RECENT_POSITION' if older else 'LATEST_TEAM_POSITION',snapshot_age_hours=age)
                selected.append(accepted)
            audit.append(dict(team=team,position=position,latest_team_snapshot=str(latest),position_known_at_utc=str(when),age_hours=age,
                selection_state=reason or ('VERIFIED_RECENT_POSITION' if older else 'LATEST_TEAM_POSITION'),
                candidate_player_ids=json.dumps(rows.gsis_id.dropna().astype(str).tolist()),accepted_rows=len(accepted),excluded_players=json.dumps(excluded)))
    frame=pd.concat(selected,ignore_index=True) if selected else d.iloc[:0].copy()
    return frame,pd.DataFrame(audit)
