"""Exact seven-input score-history baseline contract, separate from production."""
import hashlib,json
import numpy as np
import pandas as pd
from nfl2026.research.team_phase7 import BASE,poisson_win,calibrated_probability
VERSION='causal-score-history8-v1'
FEATURES=list(BASE)
def schema_hash():return hashlib.sha256(json.dumps(dict(version=VERSION,features=FEATURES),sort_keys=True).encode()).hexdigest()

def score_history_inputs(games,*,asof=None,embargo_hours=48):
    required={'game_id','season','week','home_team','away_team','kickoff_utc','home_score','away_score'}
    if not required.issubset(games) or games.game_id.duplicated().any():raise ValueError('Exact score-history fixture schema required')
    d=games.copy()
    if any(pd.Timestamp(t).tzinfo is None for t in d.kickoff_utc):raise ValueError('Explicit kickoff timezone required')
    d['kickoff_utc']=pd.to_datetime(d.kickoff_utc,utc=True);now=pd.Timestamp(asof) if asof is not None else None
    if now is not None and now.tzinfo is None:raise ValueError('Explicit as-of timezone required')
    rows=[];audit=[];history={}
    for kickoff,block in d.sort_values(['kickoff_utc','game_id']).groupby('kickoff_utc',sort=True):
        pending=[]
        for g in block.itertuples(index=False):
            if g.game_id!=f'{int(g.season)}_{int(g.week):02d}_{g.away_team}_{g.home_team}' or g.away_team==g.home_team:raise ValueError('Wrong fixture/team/opponent/week')
            cutoff=min(kickoff-pd.Timedelta(minutes=1),now) if now is not None else kickoff-pd.Timedelta(minutes=1)
            r=dict(game_id=g.game_id,home_advantage=1.) # Existing named field is a constant/intercept, not a venue observation.
            for side,other in [('home','away'),('away','home')]:
                team=getattr(g,side+'_team');old=[h for h in history.get(team,[]) if h['available']<cutoff][-8:]
                r[side+'_pf8']=float(np.mean([h['pf'] for h in old])) if old else np.nan
                r[side+'_pa8']=float(np.mean([h['pa'] for h in old])) if old else np.nan
                r[side+'_history_n']=len(old)
                audit.append(dict(game_id=g.game_id,team=team,cutoff_utc=cutoff.isoformat(),history_games=[h['game_id'] for h in old],max_available_proxy_utc=max((h['available'] for h in old),default=pd.NaT),
                    temporal_pass=all(h['available']<cutoff for h in old),publication_certified=False))
                score=getattr(g,side+'_score');against=getattr(g,other+'_score')
                if pd.notna(score) and pd.notna(against):pending.append((team,dict(game_id=g.game_id,pf=float(score),pa=float(against),available=kickoff+pd.Timedelta(hours=embargo_hours))))
            rows.append(r)
        for team,value in pending:history.setdefault(team,[]).append(value)
    return pd.DataFrame(rows)[['game_id',*FEATURES]],pd.DataFrame(audit)

def predict_research(package,frame):
    if package.get('feature_version')!=VERSION or package.get('schema_sha256')!=schema_hash() or package.get('features')!=FEATURES:raise ValueError('Baseline package contract mismatch')
    if list(frame.columns)!=FEATURES:raise ValueError('Named baseline feature order/schema mismatch')
    if np.isinf(frame.to_numpy(dtype=float)).any():raise ValueError('Infinite baseline input')
    h=np.clip(package['home'].predict(frame),.1,60);a=np.clip(package['away'].predict(frame),.1,60);p=poisson_win(h,a)
    if package.get('calibrator') is not None:p=calibrated_probability(package['calibrator'],p)
    return h,a,p
