"""Dated QB role and efficiency contract, deliberately separate from v2 weights."""
import numpy as np
import pandas as pd
from nfl2026.features.qb_identity_archive import valid_gsis

VERSION='qb-pregame-v3'
UNAVAILABLE={'IR','RES','OUT','PUP','INACTIVE','SUSPENDED','RELEASED','WAIVED','PRACTICE_SQUAD'}


def timestamp(value):
    t=pd.Timestamp(value)
    if pd.isna(t) or t.tzinfo is None:raise ValueError('Dated timezone-aware QB source required')
    return t


def role_state(context,prior_roles,cutoff):
    cutoff=timestamp(cutoff);pid=context.get('qb_player_id',context.get('projected_qb_id'))
    if not valid_gsis(pid) or not context.get('team'):raise ValueError('QB player/team identity unresolved')
    if context.get('source_state')!='VALID' or context.get('injury_source_state')!='VALID':raise ValueError('QB critical source missing/conflicting')
    error=context.get('reconciliation_error')
    active=context.get('active_roster_match')
    if (pd.notna(error) and str(error).strip()) or not isinstance(active,(bool,np.bool_)) or not active:raise ValueError('QB roster/identity contradiction')
    for key in ['known_at_utc','retrieved_at_utc']:
        if timestamp(context.get(key))>cutoff:raise ValueError('Future QB source')
    health=str(context.get('injury_status','UNKNOWN')).upper()
    if health in UNAVAILABLE:raise ValueError('Selected QB unavailable: '+health)
    if context.get('status') not in ['EXPECTED','CONFIRMED'] or context.get('confidence')!='HIGH':raise ValueError('QB starter uncertain')
    state='CONFIRMED' if context.get('starter_confirmed') is True else 'EXPECTED_HIGH'
    if health in ['QUESTIONABLE','DOUBTFUL']:state='LIMITED_OR_UNCERTAIN'
    p=prior_roles.copy()
    if len(p):
        for col in ['known_at_utc','kickoff_utc']:p[col]=pd.to_datetime(p[col],utc=True,errors='coerce')
        p=p[p.team.eq(context['team'])&p.known_at_utc.le(cutoff)&p.known_at_utc.lt(p.kickoff_utc)&p.kickoff_utc.lt(cutoff)&p.game_id.ne(context['game_id'])&p.source_state.eq('VALID')]
        p=p.loc[p.qb_player_id.map(valid_gsis).astype(bool)]
        for col,accepted in [('status',['EXPECTED','CONFIRMED']),('confidence',['HIGH'])]:
            if col not in p:p=p.iloc[:0]
            else:p=p[p[col].isin(accepted)]
        p=p.sort_values(['kickoff_utc','known_at_utc']).drop_duplicates('game_id',keep='last').tail(4)
    previous=p.qb_player_id.iloc[-1] if len(p) else None;established=None
    if len(p):
        counts=p.qb_player_id.value_counts();leaders=counts[counts.eq(counts.max())]
        if len(leaders)==1 and counts.max()>=2:established=leaders.index[0]
    return dict(player_id=pid,team=context['team'],starter_state=state,injury_status=health,
        health_is_assumed_healthy=False,previous_expected_qb_id=previous,established_qb_id=established,
        changed_from_previous_expected=(pid!=previous) if previous else None,
        genuine_replacement=(pid!=established) if established else None,
        role_provenance='prior frozen pregame expected-starter evidence; never actual participation',
        role_state='RESOLVED' if established else 'INSUFFICIENT_OR_TIED_PRIOR_ROLE',known_at_utc=context['known_at_utc'],
        retrieved_at_utc=context['retrieved_at_utc'],active_roster_match=bool(active),
        source_state=context['source_state'],injury_source_state=context['injury_source_state'],
        source_reference=context.get('official_evidence',context.get('source_url',context.get('source',''))),
        source=context.get('source',''))


def efficiency_features(player_id,history,cutoff,*,research=False,prior_dropbacks=200):
    """Individual observed efficiency, separate from any starter assertion."""
    if not valid_gsis(player_id):raise ValueError('QB GSIS identity missing')
    cutoff=timestamp(cutoff);result={'player_id':player_id};h=history.copy()
    required={'player_id','team','kickoff_utc','observed_end_utc','source_known_at_utc','dropbacks','epa_sum','successes','pass_yards','interceptions','sacks','rush_attempts','rush_yards','rush_epa'}
    if not required.issubset(h):raise ValueError('QB performance source contract incomplete')
    for c in ['kickoff_utc','observed_end_utc','source_known_at_utc']:h[c]=pd.to_datetime(h[c],utc=True,errors='coerce')
    h=h[h.kickoff_utc.lt(cutoff)&h.observed_end_utc.lt(cutoff)]
    proven=h.source_known_at_utc.notna()&h.source_known_at_utc.le(cutoff)
    unproved=int((~proven).sum())
    if not research:h=h[proven]
    personal=h[h.player_id.eq(result['player_id'])].sort_values('kickoff_utc');recent=personal.tail(8)
    result.update(feature_version=VERSION,career_games=len(personal),recent_games=len(recent),publication_gap_rows=unproved,
        strict_source_coverage=unproved==0,model_use='RESEARCH_ONLY_NOT_REGISTERED',feature_cutoff_utc=cutoff.isoformat())
    league=h[~h.player_id.eq(result['player_id'])];league_db=league.dropbacks.sum()
    rates={'epa_sum':'epa_per_dropback','successes':'success_rate','pass_yards':'yards_per_dropback','interceptions':'interception_rate','sacks':'sack_rate'}
    for label,group in [('career',personal),('recent8',recent)]:
        db=float(group.dropbacks.sum());result[label+'_dropbacks']=db
        for numerator,name in rates.items():
            # No-history identities remain missing. Small-history shrinkage is
            # explicit research, using only earlier-game league observations.
            prior=float(league[numerator].sum()/league_db) if league_db>0 else np.nan
            result[label+'_'+name]=float((group[numerator].sum()+prior_dropbacks*prior)/(db+prior_dropbacks)) if db>0 and np.isfinite(prior) else np.nan
        ra=float(group.rush_attempts.sum());result[label+'_rush_attempts']=ra
        result[label+'_rush_yards_per_attempt']=float(group.rush_yards.sum()/ra) if ra>0 else np.nan
        result[label+'_rush_epa_per_attempt']=float(group.rush_epa.sum()/ra) if ra>0 else np.nan
    db=result['career_dropbacks'];result['history_state']='NO_HISTORY_NO_GENERIC_RATING' if not db else 'SMALL_SAMPLE_RESEARCH_SHRINKAGE' if db<prior_dropbacks else 'OBSERVED_HISTORY'
    result['shrinkage_prior_dropbacks']=prior_dropbacks;result['shrinkage_certified']=False
    return result


def build_features(context,history,prior_roles,cutoff,team_context=None,*,research=False,prior_dropbacks=200):
    """Only role-validated contexts; no v3 input is assigned a v2 coefficient."""
    result=role_state(context,prior_roles,cutoff)
    result.update(efficiency_features(result['player_id'],history,cutoff,research=research,prior_dropbacks=prior_dropbacks))
    if team_context:
        result.update({f'team_{k}':v for k,v in team_context.items()})
        epa=team_context.get('pass_epa')
        result['QB_vs_team_EPA_difference']=result['recent8_epa_per_dropback']-epa if epa is not None else np.nan
    result['production_ready']=False
    result['blockers']=['v3 features require chronological model/uncertainty certification; not v2-compatible coefficients']
    if result['publication_gap_rows']:result['blockers'].append('historical source-publication/end-time evidence incomplete')
    if result['starter_state']=='LIMITED_OR_UNCERTAIN':result['blockers'].append('QB playing status/limitation unresolved')
    if result['injury_status']=='UNKNOWN':result['blockers'].append('health is UNKNOWN, never relabelled healthy')
    if result['genuine_replacement'] is None:result['blockers'].append('established role not sufficiently proved')
    return result
