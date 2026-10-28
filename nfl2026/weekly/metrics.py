"""Explicit populations and exact fixture joins. Missing labels never become zeros."""
import numpy as np
import pandas as pd
from nfl2026.research.frozen_evaluation import score_matched

KEYS=['game_id','player_id','team']
TARGETS={'passing_yards':'expected_pass_yards','attempts':'expected_pass_attempts','rushing_yards':'expected_rush_yards',
         'carries':'expected_carries','receiving_yards':'expected_receiving_yards','receptions':'expected_receptions',
         'targets':'expected_targets','anytime_offensive_td':'reconciled_td_probability'}

def probability_metrics(actual,predicted):
    y=np.asarray(actual,float);p=np.asarray(predicted,float)
    if len(y)!=len(p) or not len(y) or not np.isfinite(np.r_[y,p]).all() or not np.isin(y,[0,1]).all() or ((p<0)|(p>1)).any():raise ValueError('Explicit binary labels and valid probabilities required')
    clipped=np.clip(p,1e-15,1-1e-15);bins=[]
    for i in range(10):
        mask=(p>=i/10)&((p<(i+1)/10) if i<9 else (p<=1))
        bins.append(dict(lower=i/10,upper=(i+1)/10,rows=int(mask.sum()),predicted=float(p[mask].mean()) if mask.any() else None,observed=float(y[mask].mean()) if mask.any() else None))
    return dict(rows=len(y),brier=float(np.mean((p-y)**2)),log_loss=float(-np.mean(y*np.log(clipped)+(1-y)*np.log1p(-clipped))),
        bias=float(np.mean(p-y)),ece=float(sum(b['rows']*abs(b['predicted']-b['observed']) for b in bins if b['rows'])/len(y)),calibration=bins)

def join_player_labels(predictions,actuals):
    if not set(KEYS).issubset(predictions) or not set(KEYS).issubset(actuals):raise ValueError('Exact fixture/player/team keys required')
    if predictions[KEYS].isna().any().any() or predictions.duplicated(KEYS).any() or actuals.duplicated(KEYS).any():raise ValueError('Missing or duplicate forecast/label identity')
    extra=[c for c in actuals if c not in KEYS];renamed=actuals.rename(columns={c:'actual__'+c for c in extra})
    joined=predictions.merge(renamed,on=KEYS,how='left',validate='one_to_one',indicator='label_match')
    opponent='actual__opponent' if 'actual__opponent' in joined else 'actual__opponent_team'
    if 'opponent' in predictions and opponent in joined:
        matched=joined.label_match.eq('both')&joined[opponent].notna()
        if joined.loc[matched,'opponent'].ne(joined.loc[matched,opponent]).any():raise ValueError('Player/opponent label mismatch')
    for clock in ['season','week']:
        if clock in predictions and 'actual__'+clock in joined:
            mask=joined.label_match.eq('both')&joined['actual__'+clock].notna()
            if joined.loc[mask,clock].ne(joined.loc[mask,'actual__'+clock]).any():raise ValueError('Player label season/week mismatch')
    return joined

def player_metrics(predictions,actuals):
    joined=join_player_labels(predictions,actuals);rows=[];calibration=[]
    groups=[('ALL_FROZEN',joined)]
    if 'role_status' in joined:groups += [('FROZEN_EXPECTED_STARTER',joined[joined.role_status.eq('EXPECTED_STARTER')]),('FROZEN_OTHER',joined[~joined.role_status.eq('EXPECTED_STARTER')])]
    if 'position' in joined:groups += [(str(p),joined[joined.position.eq(p)]) for p in sorted(joined.position.dropna().unique())]
    for segment,part in groups:
        for target,pred in TARGETS.items():
            label='actual__'+target
            pv=pd.to_numeric(part[pred],errors='coerce') if pred in part else pd.Series(np.nan,index=part.index)
            av=pd.to_numeric(part[label],errors='coerce') if label in part else pd.Series(np.nan,index=part.index)
            valid=np.isfinite(pv)&np.isfinite(av);cohort=part.loc[valid].copy();base=dict(segment=segment,target=target,frozen_rows=len(part),prediction_rows=int(np.isfinite(pv).sum()),
                matched_identity_rows=int(part.label_match.eq('both').sum()),matched_label_rows=int(np.isfinite(av).sum()),evaluated_rows=int(valid.sum()),
                unknown_label_rows=int((~np.isfinite(av)).sum()),fixtures=int(cohort.game_id.nunique()),status='MATCHED_COHORT' if valid.any() else 'UNSUPPORTED_OR_UNRESOLVED')
            if valid.any():
                if target=='anytime_offensive_td':
                    m=probability_metrics(av[valid],pv[valid]);bins=m.pop('calibration');base.update({k:v for k,v in m.items() if k!='rows'})
                    calibration += [dict(segment=segment,target=target,**b) for b in bins]
                else:
                    # Reuse the existing strict scorer AFTER explicitly fixing the cohort.
                    c=cohort[KEYS].copy();c['forecast']=pv[valid].to_numpy();a=cohort[KEYS].copy();a['outcome']=av[valid].to_numpy()
                    _,m=score_matched(c,a,KEYS,'forecast','outcome');base.update({k:v for k,v in m.items() if k!='rows'})
            rows.append(base)
    return pd.DataFrame(rows),joined,pd.DataFrame(calibration)

def team_metrics(forecasts,actuals):
    keys=['game_id'];required=['game_id','home_team','away_team','home_score','away_score']
    if forecasts.game_id.duplicated().any() or actuals.game_id.duplicated().any():raise ValueError('Duplicate team fixture')
    d=forecasts.merge(actuals[required],on=keys,how='left',suffixes=('','_actual'),validate='one_to_one')
    for k in ['home_team','away_team']:
        if d[k].ne(d[k+'_actual']).any():raise ValueError('Team/opponent identity mismatch')
    cols=['home_expected_points','away_expected_points','home_score','away_score','home_win_probability']
    if not np.isfinite(d[cols].to_numpy(float)).all():raise ValueError('No partial team result or missing forecast imputation')
    if (d[['home_score','away_score']]<0).any().any():raise ValueError('Negative actual score')
    d['home_error']=d.home_expected_points-d.home_score;d['away_error']=d.away_expected_points-d.away_score
    d['score_abs_error']=(d.home_error.abs()+d.away_error.abs())/2;d['margin_error']=d.home_error-d.away_error;d['total_error']=d.home_error+d.away_error
    d['actual_margin']=d.home_score-d.away_score;d['actual_winner']=np.where(d.actual_margin>0,d.home_team,np.where(d.actual_margin<0,d.away_team,'TIE'))
    decisive=d[d.actual_margin.ne(0)];p=probability_metrics(decisive.actual_margin.gt(0),decisive.home_win_probability) if len(decisive) else {}
    metric=dict(fixtures=len(d),team_rows=2*len(d),decisive_games=len(decisive),ties=int(d.actual_margin.eq(0).sum()),score_mae=float(d.score_abs_error.mean()),
        margin_mae=float(d.margin_error.abs().mean()),total_mae=float(d.total_error.abs().mean()),home_bias=float(d.home_error.mean()),away_bias=float(d.away_error.mean()),
        margin_bias=float(d.margin_error.mean()),total_bias=float(d.total_error.mean()),winner_correct=int((decisive.home_win_probability.ge(.5)==decisive.actual_margin.gt(0)).sum()),
        winner_accuracy=float((decisive.home_win_probability.ge(.5)==decisive.actual_margin.gt(0)).mean()) if len(decisive) else None,
        win_brier=p.get('brier'),win_log_loss=p.get('log_loss'),win_ece=p.get('ece'),probability_cohort='DECISIVE_GAMES_ONLY',score_semantics='CONTINUOUS_EXPECTED_POINTS_DIFFERENCE_AND_SUM')
    groups=[]
    for name,error in [('HOME',d.home_error),('AWAY',d.away_error)]:groups.append(dict(segment=name,team_rows=len(error),mae=float(error.abs().mean()),bias=float(error.mean())))
    fav=pd.concat([d.loc[d.home_win_probability.gt(.5),'home_error'],d.loc[d.home_win_probability.lt(.5),'away_error']]);dog=pd.concat([d.loc[d.home_win_probability.lt(.5),'home_error'],d.loc[d.home_win_probability.gt(.5),'away_error']])
    for name,e in [('FROZEN_MODEL_FAVOURITE',fav),('FROZEN_MODEL_UNDERDOG',dog)]:groups.append(dict(segment=name,team_rows=len(e),mae=float(e.abs().mean()) if len(e) else None,bias=float(e.mean()) if len(e) else None))
    if 'model_margin' in d:metric['original_distribution_margin_mae']=float((d.model_margin-d.actual_margin).abs().mean())
    if 'model_total' in d:metric['original_distribution_total_mae']=float((d.model_total-d.home_score-d.away_score).abs().mean())
    return metric,d,p.get('calibration',[]),groups

def quarter_metrics(predictions,labels):
    keys=['game_id','team'];cols=['q1','q2','q3','q4']
    if predictions.duplicated(keys).any() or labels.duplicated(keys).any():raise ValueError('Duplicate quarter origin')
    d=predictions.merge(labels[keys+cols],on=keys,how='left',validate='one_to_one');error=d[['pred_'+c for c in cols]].to_numpy(float)-d[cols].to_numpy(float)
    if not np.isfinite(error).all():raise ValueError('Unresolved quarter labels')
    counts=d.groupby('game_id').team.nunique()
    if not counts.eq(2).all():raise ValueError('Both teams required for game-level quarter evaluation')
    first=pd.Series(error[:,:2].sum(axis=1),index=d.index).groupby(d.game_id).sum();second=pd.Series(error[:,2:].sum(axis=1),index=d.index).groupby(d.game_id).sum()
    result=dict(fixtures=d.game_id.nunique(),team_rows=len(d),dependent_quarter_cells=4*len(d),quarter_mae=float(np.abs(error).mean()),quarter_bias=float(error.mean()),
        first_half_game_mae=float(first.abs().mean()),second_half_game_mae=float(second.abs().mean()),first_half_game_bias=float(first.mean()),second_half_game_bias=float(second.mean()),
        calibration_status='UNSUPPORTED_WITHOUT_FROZEN_PREDICTIVE_INTERVALS_OR_DISTRIBUTIONS')
    result.update({c+'_mae':float(np.abs(error[:,i]).mean()) for i,c in enumerate(cols)});result.update({c+'_bias':float(error[:,i].mean()) for i,c in enumerate(cols)})
    # Uncertainty is clustered by fixture, never by the dependent eight quarters.
    per=pd.DataFrame(dict(game_id=d.game_id,quarter_abs=np.abs(error).mean(axis=1))).groupby('game_id').quarter_abs.mean().to_numpy()
    if len(per)>1:
        rng=np.random.default_rng(20261027);samples=per[rng.integers(0,len(per),(1000,len(per)))].mean(axis=1);result['fixture_bootstrap_95_ci']=[float(x) for x in np.quantile(samples,[.025,.975])]
    else:result['fixture_bootstrap_95_ci']=None
    return result

def deduplicate_origins(rows,keys,value_columns):
    """Equivalent copied origins count once; conflicting copies fail."""
    if 'forecast_origin_id' not in rows:raise ValueError('Immutable origin ID required')
    by=['forecast_origin_id',*keys]
    for _,g in rows.groupby(by,dropna=False):
        if len(g)>1 and any(g[c].nunique(dropna=False)>1 for c in value_columns):raise ValueError('Conflicting reused forecast origin')
    return rows.drop_duplicates(by).copy()
