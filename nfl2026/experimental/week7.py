"""Fail-closed source and evaluation contracts for the separate Week 7 lab."""
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import numpy as np
import pandas as pd
from nfl2026.features.qb_pregame_v3 import role_state

VERSION='week7-experimental-v1'
OFFICIAL='phase3_5-production-20261021T210639Z-e0207002'
LABEL='EXPERIMENTAL — NOT OFFICIAL PREDICTIONS'


def qb_check(context,roles,now):
    info=role_state(context,roles,now)
    if info['starter_state']=='LIMITED_OR_UNCERTAIN':raise ValueError('Questionable/limited QB availability unresolved')
    if pd.Timestamp(now)-pd.Timestamp(context['known_at_utc'])>pd.Timedelta(hours=36):raise ValueError('QB depth evidence exceeds 36h freshness limit')
    return info


def fixture_reason(row,now):
    if row['eligibility_state']!='PRE_KICKOFF':return row['eligibility_state']+': '+row['eligibility_reason']
    if pd.Timestamp(now)>=pd.Timestamp(row['kickoff_utc'])-pd.Timedelta(minutes=1):return 'Conservative research cutoff reached (one minute before kickoff)'
    return ''


def quarter_allocation(total,shares):
    shares=np.asarray(shares,dtype=float)
    if total<0 or len(shares)!=5 or not np.isfinite(shares).all() or (shares<0).any() or not np.isclose(shares.sum(),1):raise ValueError('Invalid quarter allocation inputs')
    ot=float(total*shares[4]);reg=float(total-ot);q=reg*shares[:4]/shares[:4].sum()
    return dict(**{f'q{i+1}':float(v) for i,v in enumerate(q)},first_half=float(q[:2].sum()),second_half=float(q[2:].sum()),
        regulation_total=reg,expected_ot_points=ot,expected_final=float(total),overtime_probability=None,
        overtime_limitation='Historical OT point share only; no validated fixture-level 2026 overtime probability')


def encode_json(value):
    def clean(x):
        if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
        if isinstance(x,(list,tuple)):return [clean(v) for v in x]
        if isinstance(x,np.generic):return clean(x.item())
        if isinstance(x,float) and not np.isfinite(x):return None
        if isinstance(x,(datetime,pd.Timestamp)):return x.isoformat()
        return x
    return json.dumps(clean(value),indent=2,ensure_ascii=False,allow_nan=False)


def immutable_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf8') as handle:handle.write(data)


def evaluate_frames(scores,passing,quarters,actual_games,actual_passing,actual_quarters):
    """Explicitly supplied final actuals only; no official ledger or implicit zeros."""
    if actual_games.game_id.duplicated().any() or not actual_games.status.eq('FINAL').all():raise ValueError('Unique FINAL actual games required')
    if not set(actual_games.game_id).issubset(set(scores.game_id)):raise ValueError('Actual games outside this frozen experiment')
    for frame,cols in [(actual_games,['actual_home_score','actual_away_score']),(actual_passing,['actual_passing_yards']),
        (actual_quarters,['actual_q'+str(i) for i in range(1,5)])]:
        if not np.isfinite(frame[cols].to_numpy(dtype=float)).all():raise ValueError('Explicit finite actual labels required; never silently drop missing outcomes')
    if 'kickoff_utc' in scores:
        kickoff=pd.to_datetime(scores.set_index('game_id').loc[actual_games.game_id,'kickoff_utc'],utc=True)
        if kickoff.ge(pd.Timestamp.now(tz='UTC')).any():raise ValueError('Cannot evaluate future fixtures as completed games')
    results=[]
    for name in ['qb','form']:
        selected=scores[scores[name+'_state'].eq('READY')]
        joined=selected.merge(actual_games,on='game_id',validate='one_to_one')
        for candidate in ['official',name]:
            if joined.empty:continue
            he=joined[candidate+'_home_score']-joined.actual_home_score;ae=joined[candidate+'_away_score']-joined.actual_away_score
            record=dict(target='team_score',matched_population=name,candidate=candidate,games=len(joined),forecast_games=len(selected),
                forecast_population_total=len(scores),forecast_coverage=float(len(selected)/len(scores)),actuals_match_coverage=float(len(joined)/len(selected)),
                team_mae=float(pd.concat([he,ae]).abs().mean()),margin_mae=float((he-ae).abs().mean()),total_mae=float((he+ae).abs().mean()),
                home_bias=float(he.mean()),away_bias=float(ae.mean()),experimental_probability_calibration='UNAVAILABLE: no experimental win probabilities emitted')
            non_tie=joined.actual_home_score.ne(joined.actual_away_score);y=joined.actual_home_score.gt(joined.actual_away_score)
            record['winner_accuracy']=float(((joined[candidate+'_home_score']>joined[candidate+'_away_score'])[non_tie]==y[non_tie]).mean()) if non_tie.any() else None
            if candidate=='official' and 'official_home_win_probability' in joined and non_tie.any():
                p=joined.loc[non_tie,'official_home_win_probability'].clip(1e-8,1-1e-8);observed=y[non_tie].astype(float)
                record.update(brier=float(((p-observed)**2).mean()),log_loss=float((-observed*np.log(p)-(1-observed)*np.log(1-p)).mean()),
                    calibration_mean_probability=float(p.mean()),calibration_observed_home_win_rate=float(observed.mean()))
            results.append(record)
    if actual_passing.duplicated(['game_id','player_id']).any():raise ValueError('Duplicate passing actuals')
    j=passing[passing.state.eq('READY')].merge(actual_passing,on=['game_id','player_id'],validate='one_to_one')
    j=j[j.game_id.isin(actual_games.game_id)]
    if len(j):
        error=j.expected_passing_yards-j.actual_passing_yards
        available=int(passing.state.eq('READY').sum())
        results.append(dict(target='QB_passing_yards',rows=len(j),forecast_rows=available,forecast_population_total=len(passing),forecast_coverage=float(available/len(passing)),actuals_match_coverage=float(len(j)/available),mae=float(error.abs().mean()),bias=float(error.mean()),interval_calibration='UNAVAILABLE: no calibrated intervals emitted'))
    if actual_quarters.duplicated(['game_id','team']).any():raise ValueError('Duplicate quarter actuals')
    j=quarters[quarters.state.eq('READY')].merge(actual_quarters,on=['game_id','team'],validate='one_to_one');j=j[j.game_id.isin(actual_games.game_id)]
    if len(j):
        errors=np.column_stack([j['q'+str(q)]-j['actual_q'+str(q)] for q in range(1,5)])
        available=int(quarters.state.eq('READY').sum())
        results.append(dict(target='team_quarters',rows=len(j),forecast_rows=available,forecast_population_total=len(quarters),forecast_coverage=float(available/len(quarters)),actuals_match_coverage=float(len(j)/available),mae=float(np.abs(errors).mean()),bias=float(errors.mean()),
            first_half_mae=float(np.abs(errors[:,:2].sum(axis=1)).mean()),second_half_mae=float(np.abs(errors[:,2:].sum(axis=1)).mean())))
    return results
