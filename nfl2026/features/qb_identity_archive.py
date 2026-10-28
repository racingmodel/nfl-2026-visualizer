"""Recover identity metadata without changing any archived numerical feature."""
import re
import numpy as np
import pandas as pd

RATINGS=['qb_epa_per_dropback','qb_success_rate','qb_yards_per_dropback','qb_interception_rate','qb_sack_rate']
IDENTITIES=[f'{s}_{c}' for s in ['home','away'] for c in ['expected_qb_id','expected_qb_name','qb_source_reliability']]


def valid_gsis(value):
    return isinstance(value,str) and re.fullmatch(r'00-\d{7}',value) is not None


def require_identity_archive(historical):
    missing=set(IDENTITIES)-set(historical)
    if missing:raise ValueError('QB identity archive incomplete: '+', '.join(sorted(missing)))
    for side in ['home','away']:
        ids=historical[side+'_expected_qb_id']
        if ids.empty or ids.isna().any() or not ids.map(valid_gsis).all():raise ValueError('Historical QB GSIS identity invalid or unresolved rows')
        for field in ['expected_qb_name','qb_source_reliability']:
            values=historical[side+'_'+field]
            if values.isna().any() or values.astype(str).str.strip().eq('').any():raise ValueError('Historical QB metadata incomplete: '+side+'_'+field)


def latest_qb_state(historical):
    """Last timestamp across both sides, rather than away rows overwriting home."""
    require_identity_archive(historical)
    parts=[]
    for side in ['home','away']:
        b=historical[['kickoff_utc',side+'_expected_qb_id',*[side+'_'+c for c in RATINGS]]].copy()
        b.columns=['kickoff_utc','player_id',*RATINGS];parts.append(b)
    d=pd.concat(parts,ignore_index=True);d['time']=pd.to_datetime(d.kickoff_utc,utc=True)
    d=d[d.player_id.notna()].sort_values(['time','player_id']).drop_duplicates('player_id',keep='last')
    return {r.player_id:{c:getattr(r,c) for c in RATINGS} for r in d.itertuples()}


def validate_qb_population(frame):
    ratings=[f'{s}_{c}' for s in ['home','away'] for c in RATINGS]
    if frame[ratings].isna().any().any():raise ValueError('Unexpected missing QB rating population; no silent median-only production')
    flags=frame[['home_qb_replacement','away_qb_replacement']].to_numpy().ravel()
    if not np.isin(flags,[0.,1.]).all():raise ValueError('QB replacement flags invalid/unknown')
    if len(flags)>=16 and np.all(flags==1):raise ValueError('Implausible all-replacement QB population requires source review')
    return True


def restore_metadata(settings,historical):
    """Use the original training builder and verify numerical parity first.

    This does not certify historic publication/eligibility, refit weights or
    rewrite the prepared production archive. Output belongs in research only.
    """
    from nfl2026.features.quarterback import augment_with_qb_features
    base=historical[['game_id','season','week','kickoff_utc','home_team','away_team']]
    reconstructed,features=augment_with_qb_features(settings,base)
    check=historical[['game_id',*features]].merge(reconstructed[['game_id',*features]],on='game_id',suffixes=('_saved','_rebuilt'),validate='one_to_one')
    diffs={c:float(np.nanmax(np.abs(check[c+'_saved']-check[c+'_rebuilt']))) for c in features}
    for c in features:
        if not np.array_equal(check[c+'_saved'].isna(),check[c+'_rebuilt'].isna()) or diffs[c]>1e-8:raise ValueError('Training reconstruction mismatch: '+c)
    result=historical.drop(columns=[c for c in IDENTITIES if c in historical]).merge(reconstructed[['game_id',*IDENTITIES]],on='game_id',validate='one_to_one')
    return result,diffs
