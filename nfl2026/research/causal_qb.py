"""Versioned team passing-performance contract; no retrospective starter labels.

Historical passer IDs are used only to measure a prior game's concentration of
dropbacks, never to assign that game's expected or starting quarterback.
"""
from __future__ import annotations
import hashlib,json
import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import PoissonRegressor
from nfl2026.research.team_phase7 import BASE,NamedMedian,poisson_win

VERSION='causal-team-qb-boxscore-v2'
RATES={'epa_per_dropback':'epa','success_rate':'success','yards_per_dropback':'yards_gained','interception_rate':'interception','sack_rate':'sack'}
QB=['yards_per_dropback','interception_rate','sack_rate','dropbacks','history_games','passer_concentration']
FEATURES=[*BASE,*[f'{side}_past_qb_{name}_8' for side in ['home','away'] for name in QB]]
PBP_COLUMNS=['game_id','posteam','defteam','play_type','passer_player_id','pass_attempt','sack','epa','success','yards_gained','interception','qb_kneel','qb_spike','two_point_attempt']

def schema_hash():return hashlib.sha256(json.dumps(dict(version=VERSION,features=FEATURES),sort_keys=True).encode()).hexdigest()

def aggregate_passing_performance(pbp,schedule):
    """Do not impute missing play outcomes, IDs or final labels to zero."""
    if not set(PBP_COLUMNS).issubset(pbp):raise ValueError('Passing outcome schema incomplete')
    if schedule.game_id.duplicated().any():raise ValueError('Duplicate schedule fixture')
    sides=pd.concat([schedule[['game_id','home_team','away_team']].rename(columns={'home_team':'team','away_team':'opponent'}),schedule[['game_id','away_team','home_team']].rename(columns={'away_team':'team','home_team':'opponent'})])
    d=pbp[pbp.game_id.isin(schedule.game_id)&pbp.posteam.notna()&pbp.play_type.ne('no_play')&pbp.qb_kneel.ne(1)&pbp.qb_spike.ne(1)&pbp.two_point_attempt.ne(1)&(pbp.pass_attempt.eq(1)|pbp.sack.eq(1))].copy()
    identities=d[['game_id','posteam','defteam']].drop_duplicates().merge(sides,left_on=['game_id','posteam'],right_on=['game_id','team'],how='left',validate='many_to_one')
    if identities.opponent.isna().any() or not identities.defteam.eq(identities.opponent).all():raise ValueError('Passing game/team/opponent identity mismatch')
    rows=[]
    for (gid,team),g in d.groupby(['game_id','posteam'],sort=False):
        n=len(g);row=dict(game_id=gid,team=team,dropbacks=n,observed_passer_id_rows=int(g.passer_player_id.notna().sum()))
        for output,column in RATES.items():
            values=pd.to_numeric(g[column],errors='raise')
            if np.isinf(values).any():raise ValueError('Infinite passing outcome')
            row[output+'_sum']=float(values.sum()) if values.notna().all() else np.nan
            row[output+'_valid']=int(values.notna().sum())
        shares=g.passer_player_id.value_counts(dropna=True)
        row['passer_concentration']=float(shares.max()/n) if len(shares) and g.passer_player_id.notna().all() else np.nan
        # Neither this ID nor this measurement is a starter designation.
        rows.append(row)
    return pd.DataFrame(rows)

def augment_causal_qb(base,performance,*,asof=None,embargo_hours=48):
    needed={'game_id','season','week','kickoff_utc','home_team','away_team',*BASE}
    if not needed.issubset(base) or base.game_id.duplicated().any():raise ValueError('Named exact base fixtures required')
    d=base.copy()
    if any(pd.Timestamp(t).tzinfo is None for t in d.kickoff_utc):raise ValueError('Explicit fixture timezone required')
    d['kickoff_utc']=pd.to_datetime(d.kickoff_utc,utc=True)
    if any(r.game_id!=f'{int(r.season)}_{int(r.week):02d}_{r.away_team}_{r.home_team}' or r.away_team==r.home_team for r in d.itertuples()):raise ValueError('Wrong fixture sides/week')
    if performance.duplicated(['game_id','team']).any():raise ValueError('Duplicate historical passing labels')
    sides=pd.concat([d[['game_id','home_team','kickoff_utc']].rename(columns={'home_team':'team'}),d[['game_id','away_team','kickoff_utc']].rename(columns={'away_team':'team'})])
    p=performance.merge(sides,on=['game_id','team'],how='left',validate='one_to_one')
    if p.kickoff_utc.isna().any():raise ValueError('Passing outcome team/game not in exact schedule')
    p['available_proxy_utc']=p.kickoff_utc+pd.Timedelta(hours=embargo_hours)
    by_team={team:g.sort_values(['kickoff_utc','game_id']) for team,g in p.groupby('team')};audit=[]
    now=pd.Timestamp(asof) if asof is not None else None
    if now is not None and now.tzinfo is None:raise ValueError('Explicit inference as-of timezone required')
    for index,g in d.iterrows():
        cutoff=g.kickoff_utc-pd.Timedelta(minutes=1)
        if now is not None:cutoff=min(cutoff,now)
        for side in ['home','away']:
            team=g[side+'_team'];history=by_team.get(team,p.iloc[:0]);history=history[history.available_proxy_utc.lt(cutoff)].tail(8)
            prefix=side+'_past_qb_';n=len(history);den=history.dropbacks.sum()
            d.loc[index,prefix+'history_games_8']=n
            d.loc[index,prefix+'dropbacks_8']=float(den) if n else np.nan
            for rate in RATES:
                d.loc[index,prefix+rate+'_8']=float(history[rate+'_sum'].sum()/den) if n and den>0 and history[rate+'_valid'].eq(history.dropbacks).all() else np.nan
            d.loc[index,prefix+'passer_concentration_8']=float(history.passer_concentration.mean()) if n and history.passer_concentration.notna().all() else np.nan
            audit.append(dict(game_id=g.game_id,team=team,cutoff_utc=cutoff.isoformat(),source_game_ids=history.game_id.tolist(),history_games=n,
                max_available_proxy_utc=history.available_proxy_utc.max().isoformat() if n else None,chronological_pass=bool(not n or history.available_proxy_utc.max()<cutoff),
                historical_expected_QB_identity='UNKNOWN_NOT_DERIVED_FROM_OUTCOMES',historical_medical_status='UNKNOWN',source_publication_certified=False))
    return d,pd.DataFrame(audit)

def model():return make_pipeline(NamedMedian(),StandardScaler(),PoissonRegressor(alpha=2.,max_iter=600))

def infer(payload,frame):
    if payload.get('feature_version')!=VERSION or payload.get('schema_sha256')!=schema_hash() or payload.get('features')!=FEATURES:raise ValueError('Causal QB artifact schema mismatch')
    if list(frame.columns)!=FEATURES:raise ValueError('Inference named features/order mismatch; no fallback')
    x=frame.apply(pd.to_numeric,errors='raise')
    if np.isinf(x.to_numpy()).any():raise ValueError('Infinite inference feature')
    h=np.clip(payload['home'].predict(x),.1,60);a=np.clip(payload['away'].predict(x),.1,60)
    probability=poisson_win(h,a)
    if payload.get('calibrator') is not None:
        from nfl2026.research.team_phase7 import calibrated_probability
        probability=calibrated_probability(payload['calibrator'],probability)
    return h,a,probability

def current_input_permission(evidence):
    required=['fixture_pregame','before_cutoff','injury_stage_valid','roster_certified','QB_source_certified','model_certified','model_approved','player_compatible','TD_compatible']
    blocked=[k for k in required if evidence.get(k) is not True]
    return dict(status='ELIGIBLE_FOR_IMMUTABLE_CANDIDATE' if not blocked else 'WITHHELD',blocked=blocked,automatic_activation=False,
        health_assumption='NONE',injury_impact_assumed_zero=False)
