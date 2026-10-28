"""Separate chronology-corrected legacy-algorithm research; never a model override.

Uses the legacy four numerical / three categorical feature names and five-game,
within-season means. Missing cold starts remain NA, handled natively by HGB.
The 48-hour outcome lag is a research availability proxy, not publication proof.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

NUMERIC=['pts_for_roll','pts_against_roll','mov_roll','is_home_effective']
CATEGORICAL=['team','opponent','week_bin']
FEATURES=NUMERIC+CATEGORICAL

def build_replay(games, embargo_hours=48):
    required={'game_id','season','week','home_team','away_team','kickoff_utc','home_score','away_score','location'}
    if not required.issubset(games) or games.game_id.duplicated().any():raise ValueError('Exact unique fixture schema required')
    d=games.copy()
    if any(pd.Timestamp(v).tzinfo is None for v in d.kickoff_utc):raise ValueError('Explicit kickoff timezone required')
    d['kickoff_utc']=pd.to_datetime(d.kickoff_utc,utc=True)
    history={};rows=[]
    for kickoff,block in d.sort_values(['kickoff_utc','game_id']).groupby('kickoff_utc',sort=True):
        pending=[]
        for g in block.itertuples(index=False):
            if g.game_id!=f'{int(g.season)}_{int(g.week):02d}_{g.away_team}_{g.home_team}' or g.home_team==g.away_team:raise ValueError('Wrong game/team/opponent join')
            if g.location not in {'Home','Neutral'}:raise ValueError('Unknown historical venue; no home-advantage default')
            if not np.isfinite([g.home_score,g.away_score]).all():raise ValueError('Completed outcomes required; no zero labels')
            cutoff=kickoff-pd.Timedelta(minutes=1)
            for side,other in [('home','away'),('away','home')]:
                team=getattr(g,side+'_team');opponent=getattr(g,other+'_team')
                old=[r for r in history.get((g.season,team),[]) if r['available']<cutoff][-5:]
                pf=float(np.mean([r['points'] for r in old])) if old else np.nan
                pa=float(np.mean([r['against'] for r in old])) if old else np.nan
                rows.append(dict(game_id=g.game_id,season=int(g.season),week=int(g.week),team=team,opponent=opponent,side=side,
                    kickoff_utc=kickoff,cutoff_utc=cutoff,target_points=float(getattr(g,side+'_score')),
                    pts_for_roll=pf,pts_against_roll=pa,mov_roll=pf-pa,is_home_effective=float(side=='home' and g.location=='Home'),
                    week_bin='EARLY' if g.week<=4 else 'MID' if g.week<=12 else 'LATE',
                    history_games=len(old),max_feature_available_utc=max((r['available'] for r in old),default=pd.NaT),
                    outcome_available_proxy_utc=kickoff+pd.Timedelta(hours=embargo_hours)))
                pending.append(((g.season,team),dict(points=getattr(g,side+'_score'),against=getattr(g,other+'_score'),available=kickoff+pd.Timedelta(hours=embargo_hours))))
        for key,value in pending:history.setdefault(key,[]).append(value)
    result=pd.DataFrame(rows)
    if result.duplicated(['game_id','team']).any():raise ValueError('Duplicate team fixture')
    return result

def chronological_split(rows,validation_season):
    test=rows[rows.season.eq(validation_season)].copy()
    if test.empty:raise ValueError('Missing validation season')
    cutoff=test.cutoff_utc.min()
    train=rows[rows.season.lt(validation_season)&rows.outcome_available_proxy_utc.lt(cutoff)].copy()
    if train.empty or set(train.game_id)&set(test.game_id):raise ValueError('Empty/overlapping historical split')
    if not train.outcome_available_proxy_utc.max()<cutoff:raise ValueError('Training outcome not available before validation')
    if train.groupby('game_id').size().ne(2).any() or test.groupby('game_id').size().ne(2).any():raise ValueError('Dependent fixture sides split or missing')
    return train,test

def checked_features(frame):
    if list(frame.columns)!=FEATURES:raise ValueError('Legacy challenger named input schema/order mismatch')
    for column in NUMERIC:
        values=pd.to_numeric(frame[column],errors='raise')
        if np.isinf(values).any():raise ValueError('Infinite numerical input')
    if frame[CATEGORICAL].isna().any().any():raise ValueError('Unknown categorical identity')
    return frame

def estimator():
    return Pipeline([('pre',ColumnTransformer([('num','passthrough',NUMERIC),('cat',OneHotEncoder(handle_unknown='ignore',sparse_output=False),CATEGORICAL)])),
        ('est',HistGradientBoostingRegressor(loss='poisson',learning_rate=.06,max_depth=4,max_iter=800,max_bins=255,min_samples_leaf=25,l2_regularization=.8,random_state=42,
            # Random early-stopping rows would form a nonchronological inner holdout.
            early_stopping=False))])

def legacy_weights(rows):
    base=rows.season.map({2022:.25,2023:.5,2024:.9,2025:1.8}).fillna(.8)
    ramp=np.where(rows.season.eq(2025),np.clip(1+.10*(rows.week-1),1,2.5),1)
    return np.clip(base*ramp,.10,4.5)
