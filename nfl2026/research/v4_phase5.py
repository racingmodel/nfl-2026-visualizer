"""Independent V4 certification diagnostics; never selects a production model."""
from __future__ import annotations
import re
import numpy as np
import pandas as pd
KEYS=['game_id','player_id','team']
SEGMENTS={'RB_rushing':('RB','rushing_yards'),'QB_rushing':('QB','rushing_yards'),
          'WR_receiving':('WR','receiving_yards'),'TE_receiving':('TE','receiving_yards'),
          'RB_receiving':('RB','receiving_yards'),'WR_targets':('WR','targets'),'WR_receptions':('WR','receptions')}

def strict_labels(forecasts,actuals,targets):
    """Keep original forecast keys; missing labels stay pending, never zero."""
    if forecasts.duplicated(KEYS).any() or actuals.duplicated(KEYS).any():raise ValueError('Duplicate player/team/game identity')
    if any(c not in actuals for c in targets):raise ValueError('Actual target definition unavailable')
    joined=forecasts.merge(actuals[KEYS+targets],on=KEYS,how='left',validate='one_to_one',indicator=True)
    present=joined._merge.eq('both')
    if joined.loc[present,targets].isna().any().any():raise ValueError('Present actual row has unknown target')
    if not np.isfinite(joined.loc[present,targets].to_numpy(float)).all():raise ValueError('Invalid actual values')
    return joined[present].drop(columns='_merge'),joined[~present][KEYS].copy()

def block_ci(frame,values,*,block='game_id',draws=1500):
    a=pd.DataFrame({'block':frame[block].astype(str).to_numpy(),'v':np.asarray(values,float)})
    if not np.isfinite(a.v).all() or len(a)==0:raise ValueError('CI data missing')
    g=a.groupby('block').v.agg(['sum','count']);rng=np.random.default_rng(20261027)
    if len(g)<2:return [None,None]
    idx=rng.integers(0,len(g),(draws,len(g)))
    x=g['sum'].to_numpy()[idx].sum(axis=1)/g['count'].to_numpy()[idx].sum(axis=1)
    return [float(v) for v in np.quantile(x,[.025,.975])]

def paired_point(frame,target,incumbent,candidate):
    y=frame[target].to_numpy(float);a=frame[incumbent].to_numpy(float);b=frame[candidate].to_numpy(float)
    if not np.isfinite(np.c_[y,a,b]).all():raise ValueError('Unmatched/nonfinite point comparison')
    diff=np.abs(b-y)-np.abs(a-y)
    return dict(rows=len(frame),games=frame.game_id.nunique(),incumbent_mae=float(np.abs(a-y).mean()),v4_mae=float(np.abs(b-y).mean()),
        incumbent_bias=float((a-y).mean()),v4_bias=float((b-y).mean()),mae_delta=float(diff.mean()),game_ci95=block_ci(frame,diff),
        incumbent_rmse=float(np.sqrt(((a-y)**2).mean())),v4_rmse=float(np.sqrt(((b-y)**2).mean())))

def probability_metrics(y,p):
    y=np.asarray(y,float);p=np.asarray(p,float)
    if not np.isfinite(y).all() or not np.isfinite(p).all() or not np.isin(y,[0,1]).all() or ((p<0)|(p>1)).any():raise ValueError('TD probability/label invalid')
    clipped=np.clip(p,1e-8,1-1e-8);loss=-(y*np.log(clipped)+(1-y)*np.log1p(-clipped));ece=0.
    for low in np.arange(0,1,.1):
        mask=(p>=low)&((p<low+.1) if low<.9 else (p<=1))
        if mask.any():ece+=mask.mean()*abs(p[mask].mean()-y[mask].mean())
    return dict(rows=len(y),brier=float(((p-y)**2).mean()),log_loss=float(loss.mean()),bias=float((p-y).mean()),ece10=float(ece),mean_probability=float(p.mean()),event_rate=float(y.mean()))

def receiver_quality(frame):
    """Annotate the single list; no identity removal or role promotion."""
    if frame.duplicated(KEYS).any():raise ValueError('Duplicate receiver')
    d=frame.copy();complete=d[['prior3_targets','prior8_targets','prior_measured_games']].notna().all(axis=1)
    enough=complete&d.prior_measured_games.ge(3)
    d['usage_quality']=np.select([~complete,~enough],['UNKNOWN_USAGE','LIMITED_HISTORY'],default='MEASURED_PRIOR_USAGE')
    d['supported_growth']=enough&d.prior3_targets.ge(d.prior8_targets+1)&d.prior3_targets.ge(1.2*d.prior8_targets)
    return d

def validate_target_promotion(request,gate_evidence,registry_capability):
    """Current registry cannot accept a V4 nested package as a drop-in model."""
    from nfl2026.phase7.lab import promotion_decision
    allowed={('RB','rushing_yards'),('QB','rushing_yards'),('WR','receiving_yards'),('TE','receiving_yards'),('RB','receiving_yards'),('WR','targets'),('WR','receptions'),('ALL','anytime_offensive_td')}
    if (request.get('position'),request.get('target')) not in allowed:raise ValueError('Explicit supported target/position required')
    decision=promotion_decision(gate_evidence)
    if decision['blocked_gates']:raise ValueError('Promotion gates have not passed')
    if registry_capability.get('position_target_dispatch') is not True:raise ValueError('Registered production loader lacks certified position/target dispatch')
    if not request.get('human_approval_id') or request.get('expires_valid') is not True:raise ValueError('Exact unexpired human approval required')
    if request.get('target')!='anytime_offensive_td':
        if not re.fullmatch('[0-9a-f]{64}',str(request.get('original_td_hash',''))) or request.get('unchanged_td_hash')!=request['original_td_hash']:
            raise ValueError('Yardage promotion cannot change or omit original TD identity')
    return dict(status='REVIEWABLE_TARGET_ONLY_PROPOSAL',production_selected=False,publication=False)

def lag_features(candidates,observations):
    """Inclusive past observation state exposed only48h later, never future rows."""
    keys=['game_id','player_id','team'];d=candidates.copy().reset_index(drop=True)
    if d.duplicated(keys).any() or observations.duplicated(keys).any():raise ValueError('Ambiguous feature identity')
    h=observations.copy();h['time']=pd.to_datetime(h.kickoff_utc,utc=True,format='mixed')
    d['time']=pd.to_datetime(d.kickoff_utc,utc=True,format='mixed');d['_order']=np.arange(len(d))
    h=h.sort_values(['player_id','time','game_id']).reset_index(drop=True)
    fields=['carries','targets','receptions','rushing_yards','receiving_yards','air_yards','offense_pct','red_zone_carries','inside_10_carries','inside_5_carries','red_zone_targets','inside_10_targets','inside_5_targets']
    for f in fields:
        if f not in h:h[f]=np.nan
        h['lag8_'+f]=h.groupby('player_id')[f].transform(lambda x:x.rolling(8,min_periods=1).mean())
    h['prior3_targets']=h.groupby('player_id').targets.transform(lambda x:x.rolling(3,min_periods=1).mean())
    h['prior_measured_games']=h.groupby('player_id').cumcount()+1
    h['available_proxy']=h.time+pd.Timedelta(hours=48)
    columns=['player_id','available_proxy','time','prior3_targets','prior_measured_games']+['lag8_'+f for f in fields]
    state=h[columns].rename(columns={'time':'last_history_kickoff'})
    out=pd.merge_asof(d.sort_values(['time','player_id']),state.sort_values(['available_proxy','player_id']),on=None,
        left_on='time',right_on='available_proxy',by='player_id',direction='backward',allow_exact_matches=False)
    out['air_yards_per_target']=out.lag8_air_yards/out.lag8_targets.replace(0,np.nan)
    out['prior8_targets']=out.lag8_targets
    out['source_publication_certified']=False
    if (out.last_history_kickoff.notna() & (out.last_history_kickoff.ge(out.time-pd.Timedelta(hours=48)))).any():raise ValueError('Future player outcome leakage')
    return out.sort_values('_order').drop(columns=['_order','time']).reset_index(drop=True)
