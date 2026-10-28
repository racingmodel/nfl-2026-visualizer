"""Evidence-bound replacement residual for research; no production integration."""
import math
import pandas as pd
from nfl2026.evolution.research import resolve_qb_context


def require_qb_archive_identity(frame):
    required={f'{s}_expected_qb_id' for s in ['home','away']}
    if not required.issubset(frame):raise ValueError('QB identity provenance absent; cannot build per-player career state')
    if frame[list(required)].isna().any().any():raise ValueError('Unresolved historic QB identity')
    return True


def replacement_adjustment(context,incoming,prior,pregame_plays,pass_rate,parameters):
    """Only the calibrated EPA residual rule with corroborated identities.

    Quality estimates must use identical 200-dropback shrinkage and carry dated
    source evidence. Limited/unknown health never becomes a healthy adjustment.
    """
    if any(not context.get(k) for k in ['team','cutoff_utc','known_at_utc']):raise ValueError('Dated team-specific QB evidence required')
    status=resolve_qb_context(context)
    if 'NO_ADJUSTMENT' in status:return dict(adjustment=0.,state=status,production_ready=False)
    stop=pd.Timestamp(context['cutoff_utc'])
    for evidence in [incoming,prior]:
        if evidence.get('team')!=context['team'] or not evidence.get('player_id'):raise ValueError('Quality identity/team mismatch')
        known=pd.Timestamp(evidence.get('known_at_utc'))
        if pd.isna(known) or known.tzinfo is None or known>=stop:raise ValueError('Quality source not known before cutoff')
        if evidence.get('prior_dropbacks')!=parameters['quality_prior_dropbacks']:raise ValueError('Quality shrinkage differs from calibrated component')
        if not math.isfinite(evidence.get('shrunk_epa_per_dropback',float('nan'))):raise ValueError('Missing quality evidence')
    if incoming['player_id']!=context['projected_qb_id']:raise ValueError('Incoming identity differs from resolved starter')
    if incoming['player_id']==prior['player_id']:return dict(adjustment=0.,state='UNCHANGED_QB',production_ready=False)
    if not 0<=pass_rate<=1 or not 0<pregame_plays<150:raise ValueError('Invalid pregame opportunity')
    delta=incoming['shrunk_epa_per_dropback']-prior['shrunk_epa_per_dropback']
    value=parameters['coefficient']*delta*pregame_plays*pass_rate
    return dict(adjustment=float(value),state='EVIDENCE_BOUND_REPLACEMENT_RESEARCH_ONLY',volume=pregame_plays*pass_rate,production_ready=False)
