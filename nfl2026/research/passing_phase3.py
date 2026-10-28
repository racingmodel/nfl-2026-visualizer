"""Chronological passing challenger tools. No production registration or role inference.

Historical source publication clocks are absent. The 48-hour lag is a conservative
research embargo, NOT evidence that an archived value was published then.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from nfl2026.data.passing_contract import aggregate_individual_passing

KEYS=['game_id','team','player_id']
PERSONAL=['attempts_4','attempts_8','yards_4','yards_8','completion_rate_8','ypa_8',
          'history_count','days_since_pass_game','last_team_attempt_share','opponent_adjusted_yards_4']
CONTEXT=['team_attempts_6','team_yards_6','team_pass_rate_6','team_neutral_pass_rate_6',
         'team_plays_6','team_sack_rate_6','team_hit_rate_6','opponent_yards_6',
         'opponent_attempts_6','opponent_epa_6','opponent_sack_rate_6','home']
FEATURES=PERSONAL+CONTEXT
PBP_COLUMNS=['game_id','play_id','posteam','defteam','passer_player_id','passing_yards',
             'pass_touchdown','pass_attempt','sack','two_point_attempt','play_type',
             'complete_pass','season_type','rush_attempt','qb_dropback','qb_hit','epa',
             'score_differential','qtr','qb_kneel','qb_spike']

def schedule_sides(schedule):
    required={'game_id','home_team','away_team','kickoff_utc'}
    if not required.issubset(schedule) or schedule.game_id.duplicated().any():
        raise ValueError('Invalid exact fixture schedule')
    parts=[]
    for side,other in [('home','away'),('away','home')]:
        d=schedule[['game_id',side+'_team',other+'_team','kickoff_utc']].rename(
            columns={side+'_team':'team',other+'_team':'opponent'})
        d['home']=int(side=='home');parts.append(d)
    out=pd.concat(parts,ignore_index=True)
    if out.duplicated(['game_id','team']).any() or out.team.eq(out.opponent).any():
        raise ValueError('Ambiguous fixture sides')
    return out

def aggregate_context(pbp,schedule):
    """Outcomes only. Strict lagging below is mandatory before these become X."""
    labels=aggregate_individual_passing(pbp)
    sides=schedule_sides(schedule)
    labels=labels.merge(sides,on=['game_id','team'],validate='many_to_one')
    p=pbp[pbp.posteam.notna()&pbp.play_type.ne('no_play')&pbp.two_point_attempt.ne(1)].copy()
    identity=p[['game_id','posteam','defteam']].drop_duplicates().merge(
        sides[['game_id','team','opponent']],left_on=['game_id','posteam'],right_on=['game_id','team'],
        how='left',validate='many_to_one')
    if identity.opponent.isna().any() or not identity.defteam.eq(identity.opponent).all():
        raise ValueError('PBP team/opponent mismatch')
    p['db']=p.qb_dropback.eq(1).astype(int)
    p['eligible']=((p.db.eq(1)|p.rush_attempt.eq(1))&p.qb_kneel.ne(1)&p.qb_spike.ne(1)).astype(int)
    neutral=p.score_differential.abs().le(7)&p.qtr.le(3)&p.eligible.eq(1)
    p['neutral']=neutral.astype(int);p['neutral_pass']=(neutral&p.db.eq(1)).astype(int)
    p['rate_pass']=(p.eligible.eq(1)&p.db.eq(1)).astype(int)
    p['db_epa']=p.epa.where(p.db.eq(1));p['sack_count']=p.sack.eq(1).astype(int)
    p['hit_count']=p.qb_hit.eq(1).astype(int)
    c=p.groupby(['game_id','posteam']).agg(dropbacks=('db','sum'),plays=('eligible','sum'),
        neutral=('neutral','sum'),neutral_pass=('neutral_pass','sum'),epa=('db_epa','mean'),
        sacks=('sack_count','sum'),hits=('hit_count','sum'),rate_pass=('rate_pass','sum')).reset_index().rename(columns={'posteam':'team'})
    totals=labels.groupby(['game_id','team'])[['pass_attempts','passing_yards']].sum().reset_index()
    c=c.merge(totals,on=['game_id','team'],validate='one_to_one').merge(sides,on=['game_id','team'],validate='one_to_one')
    for name,num,den in [('pass_rate','rate_pass','plays'),('neutral_pass_rate','neutral_pass','neutral'),
                         ('sack_rate','sacks','dropbacks'),('hit_rate','hits','dropbacks')]:
        c[name]=c[num]/c[den].replace(0,np.nan)
    return labels,c

def build_features(candidates,labels,context,schedule,embargo_hours=48):
    """Only earlier, embargo-cleared outcomes; never actual starter/target volume X."""
    if candidates[KEYS].isna().any().any() or candidates.duplicated(KEYS).any():raise ValueError('Missing or duplicate candidate identity')
    base=candidates.drop(columns=['opponent','home'],errors='ignore').copy()
    sides=schedule_sides(schedule)
    # Schedule clocks and sides are authoritative; reject rather than suffix/fill.
    d=base.merge(sides.drop(columns='kickoff_utc'),on=['game_id','team'],validate='many_to_one',how='left')
    clocks=base[['game_id','kickoff_utc']].drop_duplicates().merge(schedule[['game_id','kickoff_utc']],on='game_id',suffixes=('_candidate','_schedule'),validate='many_to_one')
    if not pd.to_datetime(clocks.kickoff_utc_candidate,utc=True).eq(pd.to_datetime(clocks.kickoff_utc_schedule,utc=True)).all() or d.opponent.isna().any():
        raise ValueError('Candidate fixture clock/team mismatch')
    h=labels.copy();c=context.copy()
    h['time']=pd.to_datetime(h.kickoff_utc,utc=True);c['time']=pd.to_datetime(c.kickoff_utc,utc=True)
    hi={k:v.sort_values('time') for k,v in h[h.pass_attempts.gt(0)].groupby('player_id')}
    ci={k:v.sort_values('time') for k,v in c.groupby('team')}
    defense={k:v.sort_values('time') for k,v in c.groupby('opponent')}
    for f in FEATURES:
        if f!='home':d[f]=np.nan
    d['max_outcome_kickoff_used']=None
    d['source_publication_certified']=False
    d['history_count']=0
    for i,row in d.iterrows():
        cutoff=pd.Timestamp(row.kickoff_utc)-pd.Timedelta(hours=embargo_hours)
        p=hi.get(row.player_id,h.iloc[:0]);p=p[p.time.lt(cutoff)]
        own=ci.get(row.team,c.iloc[:0]);own=own[own.time.lt(cutoff)].tail(6)
        opp=defense.get(row.opponent,c.iloc[:0]);opp=opp[opp.time.lt(cutoff)].tail(6)
        d.loc[i,'history_count']=len(p)
        used=[]
        if len(p):
            last=p.tail(8);recent=p.tail(4);used+=list(last.time)
            d.loc[i,['attempts_4','attempts_8','yards_4','yards_8','completion_rate_8','ypa_8','days_since_pass_game']]=[
                recent.pass_attempts.mean(),last.pass_attempts.mean(),recent.passing_yards.mean(),last.passing_yards.mean(),
                last.completions.sum()/last.pass_attempts.sum(),last.passing_yards.sum()/last.pass_attempts.sum(),
                (pd.Timestamp(row.kickoff_utc)-p.time.iloc[-1]).total_seconds()/86400]
            game=c[c.game_id.eq(p.game_id.iloc[-1])&c.team.eq(p.team.iloc[-1])]
            if len(game)==1 and game.pass_attempts.iloc[0]>0:
                d.loc[i,'last_team_attempt_share']=p.pass_attempts.iloc[-1]/game.pass_attempts.iloc[0]
            adjusted=[]
            for past in recent.itertuples():
                prior=defense.get(past.opponent,c.iloc[:0]);prior=prior[prior.time.lt(past.time-pd.Timedelta(hours=embargo_hours))].tail(6)
                if len(prior):adjusted.append(past.passing_yards-prior.passing_yards.mean())
            if adjusted and len(opp):d.loc[i,'opponent_adjusted_yards_4']=np.mean(adjusted)+opp.passing_yards.mean()
        if len(own):
            used+=list(own.time)
            d.loc[i,['team_attempts_6','team_yards_6','team_pass_rate_6','team_neutral_pass_rate_6','team_plays_6','team_sack_rate_6','team_hit_rate_6']]=own[['pass_attempts','passing_yards','pass_rate','neutral_pass_rate','plays','sack_rate','hit_rate']].mean().to_numpy()
        if len(opp):
            used+=list(opp.time)
            d.loc[i,['opponent_yards_6','opponent_attempts_6','opponent_epa_6','opponent_sack_rate_6']]=opp[['passing_yards','pass_attempts','epa','sack_rate']].mean().to_numpy()
        if used:
            newest=max(used)
            if newest>=cutoff:raise ValueError('Outcome entered target embargo')
            d.loc[i,'max_outcome_kickoff_used']=newest.isoformat()
    return d

class ExplicitMedian(BaseEstimator,TransformerMixin):
    """Training-only medians plus missing indicators; all-missing columns fail."""
    def fit(self,X,y=None):
        a=np.asarray(X,float)
        if np.isinf(a).any() or np.isnan(a).all(axis=0).any():raise ValueError('Unsupported feature column')
        self.medians_=np.nanmedian(a,axis=0);self.n_features_in_=a.shape[1]
        return self
    def transform(self,X):
        a=np.asarray(X,float)
        if a.ndim!=2 or a.shape[1]!=self.n_features_in_ or np.isinf(a).any():raise ValueError('Feature schema mismatch')
        missing=np.isnan(a)
        return np.concatenate([np.where(missing,self.medians_,a),missing.astype(float)],axis=1)

class ResidualCalibration:
    """Signed empirical errors from disjoint prior calibration; never MAE sigma."""
    def fit(self,frame,target,prediction,training_max,minimum=100):
        if len(frame)<minimum or frame.duplicated(KEYS).any():raise ValueError('Insufficient independent calibration')
        times=pd.to_datetime(frame.kickoff_utc,utc=True)
        if times.min()<=pd.Timestamp(training_max):raise ValueError('Calibration overlaps training')
        self.residuals=np.sort((frame[target]-frame[prediction]).to_numpy(float))
        if not np.isfinite(self.residuals).all():raise ValueError('Invalid residual')
        self.calibration_max=times.max();self.n=len(frame)
        return self
    def predict(self,prediction,kickoff):
        if pd.Timestamp(kickoff)<=self.calibration_max:raise ValueError('Calibration leakage')
        p=np.asarray(prediction,float)
        # Finite-sample split-conformal absolute residual interval, marginal only.
        rank=min(self.n,int(np.ceil((self.n+1)*.8)))
        q=np.sort(np.abs(self.residuals))[rank-1]
        return np.maximum(0,p-q),p+q
    def over(self,prediction,line,kickoff):
        if pd.Timestamp(kickoff)<=self.calibration_max:raise ValueError('Calibration leakage')
        # Smoothed empirical tail; discrete integer outcomes at half-yard lines.
        return (1+(self.residuals>line-prediction).sum())/(self.n+2)

def prospective_row(evidence,projection=None):
    """Passing UI contract never invents numbers for unsupported QB/input sources."""
    required=['fixture_pregame','identity_certified','roster_certified','qb_source_certified',
              'features_certified','model_promoted','calibration_certified']
    gaps=[k for k in required if evidence.get(k) is not True]
    if gaps or not evidence.get('player_id'):
        return dict(evidence,status='WITHHELD',gaps=gaps,projected_yards=None,projected_attempts=None,
                    interval=None,over_probability=None,fair_odds=None,bookmaker_comparison=None,actionable=False)
    if projection is None or any(not np.isfinite(projection.get(k,np.nan)) or projection[k]<0 for k in ['projected_yards','projected_attempts']):
        raise ValueError('Certified row requires finite joint projections')
    return dict(evidence,**projection,status='CERTIFIED',actionable=False)
