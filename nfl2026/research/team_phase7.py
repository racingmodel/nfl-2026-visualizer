"""Research-only team-score experiments. No registry or production writes.

Prior outcomes use a conservative 48-hour embargo. It is not proof of the
historical publication/model-vintage clocks, so the resulting study cannot
by itself confer production certification.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from scipy.stats import skellam,nbinom
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import PoissonRegressor,Ridge,LogisticRegression
from sklearn.ensemble import HistGradientBoostingRegressor
from nfl2026.research.passing_phase3 import ExplicitMedian

class NamedMedian(ExplicitMedian):
    """Research challenger contract: feature names/order are part of the schema."""
    def fit(self,X,y=None):
        if not hasattr(X,'columns') or X.columns.duplicated().any():raise ValueError('Named unique feature schema required')
        self.feature_names_=list(X.columns)
        return super().fit(X,y)
    def transform(self,X):
        if not hasattr(X,'columns') or list(X.columns)!=self.feature_names_:raise ValueError('Named feature schema/order mismatch')
        return super().transform(X)

BASE=['home_pf8','home_pa8','away_pf8','away_pa8','home_history_n','away_history_n','home_advantage']
FAMILIES={
    'rest':[f'{s}_{m}' for s in ('home','away') for m in ('rest_days','short_week','bye_gap')],
    'offensive_line':[f'{s}_{m}' for s in ('home','away') for m in ('sack8','pressure8','protection_matchup')],
    'defensive_front':[f'{s}_{m}' for s in ('home','away') for m in ('def_sack8','def_pressure8','def_rush_epa8','def_all_play_ypp8')],
    'qb_continuity':[f'{s}_{m}' for s in ('home','away') for m in ('prior_primary_passer_switch_rate','prior_primary_passer_tenure')],
    'recent_form':[f'{s}_{m}' for s in ('home','away') for m in ('pf_ew','pa_ew','opponent_adjusted_pf_ew','opponent_adjusted_pa_ew')],
    'seasonality':['week_number','early_season','late_season','home_season_history','away_season_history','home_early_form_interaction','away_early_form_interaction'],
    'tempo_matchup':[f'{s}_{m}' for s in ('home','away') for m in ('plays8','neutral_pass8','redzone_td_play_rate8','off_epa8','off_pass_epa8','matchup_epa','matchup_pass_epa')],
}
OUTCOMES=['sack_rate','pressure_rate','rush_epa','yards_per_play','plays','neutral_pass_rate','red_zone_td_rate','epa_per_play','pass_epa']

def _mean(history,key):
    values=np.asarray([r.get(key,np.nan) for r in history],dtype=float)
    return float(np.nanmean(values)) if np.isfinite(values).any() else np.nan

def _ew(history,key):
    values=np.asarray([r.get(key,np.nan) for r in history],dtype=float)
    weights=2.**(-np.arange(len(values)-1,-1,-1)/3.)
    valid=np.isfinite(values)
    return float(np.dot(values[valid],weights[valid])/weights[valid].sum()) if valid.any() else np.nan

def build_features(games,efficiency,primary_passers,embargo_hours=48):
    needed={'game_id','season','week','home_team','away_team','kickoff_utc','home_score','away_score'}
    if not needed.issubset(games) or games.game_id.isna().any() or games.game_id.duplicated().any():raise ValueError('Missing/duplicate exact fixtures')
    d=games.copy()
    if any(pd.Timestamp(t).tzinfo is None for t in d.kickoff_utc):raise ValueError('Explicit fixture timezone required')
    d['kickoff_utc']=pd.to_datetime(d.kickoff_utc,utc=True,format='mixed',errors='raise')
    if any(str(g.game_id)!=f'{int(g.season)}_{int(g.week):02d}_{g.away_team}_{g.home_team}' for g in d.itertuples()):raise ValueError('Fixture ID/team/week mismatch')
    if d.home_team.eq(d.away_team).any() or d.kickoff_utc.isna().any():raise ValueError('Invalid fixture sides/clocks')
    sides=pd.concat([d[['game_id','home_team','away_team']].rename(columns={'home_team':'team','away_team':'opponent'}),
                     d[['game_id','away_team','home_team']].rename(columns={'away_team':'team','home_team':'opponent'})])
    clocks=sides.merge(d[['game_id','kickoff_utc']],on='game_id',validate='many_to_one')
    if clocks.duplicated(['team','kickoff_utc']).any():raise ValueError('Same team has simultaneous fixtures')
    e=efficiency.copy()
    if e.duplicated(['game_id','team']).any():raise ValueError('Duplicate efficiency identities')
    joined=e[['game_id','team','opponent']].merge(sides,on=['game_id','team'],how='left',suffixes=('_e','_g'),validate='many_to_one')
    if joined.opponent_g.isna().any() or not joined.opponent_e.eq(joined.opponent_g).all():raise ValueError('Wrong team/opponent efficiency join')
    if primary_passers.duplicated(['game_id','team']).any():raise ValueError('Ambiguous primary passer history')
    if not set(map(tuple,primary_passers[['game_id','team']].to_numpy())).issubset(set(map(tuple,sides[['game_id','team']].to_numpy()))):
        raise ValueError('Passer history fixture/team mismatch')
    lookup=e.set_index(['game_id','team']).to_dict('index');qbs=primary_passers.set_index(['game_id','team']).player_id.to_dict()
    history={};rows=[];audit=[]
    # Current outcomes are appended only after all contemporaneous features.
    for kickoff,block in d.sort_values(['kickoff_utc','game_id']).groupby('kickoff_utc',sort=True):
        pending=[]
        for g in block.itertuples(index=False):
            cutoff=kickoff-pd.Timedelta(minutes=1)
            r=dict(game_id=g.game_id,season=int(g.season),week=int(g.week),kickoff_utc=kickoff.isoformat(),home_team=g.home_team,away_team=g.away_team,
                target_home_score=g.home_score,target_away_score=g.away_score,home_advantage=1.,week_number=float(g.week),early_season=float(g.week<=4),late_season=float(g.week>=15))
            prior={}
            for side,team in [('home',g.home_team),('away',g.away_team)]:
                hist=[x for x in history.get(team,[]) if x['available']<cutoff][-8:];prior[side]=hist
                current_season=[x for x in hist if x['season']==g.season]
                rest=(kickoff-current_season[-1]['kickoff']).total_seconds()/86400 if current_season else np.nan
                r.update({f'{side}_pf8':_mean(hist,'pf'),f'{side}_pa8':_mean(hist,'pa'),f'{side}_history_n':len(hist),
                    f'{side}_season_history':len(current_season),f'{side}_rest_days':rest,
                    f'{side}_short_week':float(rest<6.5) if np.isfinite(rest) else np.nan,
                    f'{side}_bye_gap':float(rest>=10) if np.isfinite(rest) else np.nan,
                    f'{side}_pf_ew':_ew(hist,'pf'),f'{side}_pa_ew':_ew(hist,'pa'),
                    f'{side}_opponent_adjusted_pf_ew':_ew(hist,'adjusted_pf'),f'{side}_opponent_adjusted_pa_ew':_ew(hist,'adjusted_pa')})
                for output,key in [('sack8','sack_rate'),('pressure8','pressure_rate'),('def_sack8','def_sack_rate'),
                    ('def_pressure8','def_pressure_rate'),('def_rush_epa8','def_rush_epa'),('def_all_play_ypp8','def_yards_per_play'),
                    ('plays8','plays'),('neutral_pass8','neutral_pass_rate'),('redzone_td_play_rate8','red_zone_td_rate'),
                    ('off_epa8','epa_per_play'),('off_pass_epa8','pass_epa'),('def_epa8','def_epa_per_play'),('def_pass_epa8','def_pass_epa')]:
                    r[f'{side}_{output}']=_mean(hist,key)
                names=[x.get('primary_passer') if pd.notna(x.get('primary_passer')) and str(x.get('primary_passer')).strip() else None for x in hist]
                pairs=[a!=b for a,b in zip(names[:-1],names[1:]) if a is not None and b is not None]
                r[f'{side}_prior_primary_passer_switch_rate']=float(np.mean(pairs)) if pairs else np.nan
                tenure=np.nan
                if names and names[-1] is not None:
                    tenure=0
                    for name in reversed(names):
                        if name is None or name!=names[-1]:break
                        tenure+=1
                r[f'{side}_prior_primary_passer_tenure']=tenure
                maxprior=max((x['available'] for x in hist),default=pd.NaT)
                audit.append(dict(game_id=g.game_id,team=team,feature_cutoff_utc=cutoff.isoformat(),historical_rows=len(hist),
                    maximum_history_available_proxy_utc=maxprior.isoformat() if pd.notna(maxprior) else None,
                    chronological_pass=bool(pd.isna(maxprior) or maxprior<cutoff),publication_certified=False))
            for side,other in [('home','away'),('away','home')]:
                r[f'{side}_protection_matchup']=r[f'{side}_pressure8']*r[f'{other}_def_pressure8']
                r[f'{side}_matchup_epa']=r[f'{side}_off_epa8']-r[f'{other}_def_epa8']
                r[f'{side}_matchup_pass_epa']=r[f'{side}_off_pass_epa8']-r[f'{other}_def_pass_epa8']
                r[f'{side}_early_form_interaction']=r['early_season']*r[f'{side}_pf8']
                team=getattr(g,side+'_team');opponent=getattr(g,other+'_team')
                own=lookup.get((g.game_id,team),{});opp=lookup.get((g.game_id,opponent),{})
                pf=float(getattr(g,side+'_score'));pa=float(getattr(g,other+'_score'))
                obs=dict(kickoff=kickoff,available=kickoff+pd.Timedelta(hours=embargo_hours),season=g.season,pf=pf,pa=pa,
                    primary_passer=qbs.get((g.game_id,team)),adjusted_pf=pf-_mean(prior[other],'pa'),adjusted_pa=pa-_mean(prior[other],'pf'))
                obs.update({m:own.get(m,np.nan) for m in OUTCOMES});obs.update({'def_'+m:opp.get(m,np.nan) for m in OUTCOMES})
                pending.append((team,obs))
            rows.append(r)
        for team,obs in pending:history.setdefault(team,[]).append(obs)
    return pd.DataFrame(rows),pd.DataFrame(audit)

def feature_sets():
    result={'POISSON_BASE':BASE}
    result.update({'POISSON_'+name.upper():BASE+features for name,features in FAMILIES.items()})
    all_features=list(dict.fromkeys(BASE+sum(FAMILIES.values(),[])))
    result.update(POISSON_ALL=all_features,RIDGE_BASE=BASE,RIDGE_ALL=all_features,HIST_BOOST_ALL=all_features)
    return result

def estimator(name):
    reg=Ridge(alpha=40.) if name.startswith('RIDGE') else HistGradientBoostingRegressor(max_iter=100,max_leaf_nodes=8,min_samples_leaf=30,l2_regularization=10.,random_state=20261027) if name.startswith('HIST') else PoissonRegressor(alpha=2.,max_iter=600)
    return make_pipeline(NamedMedian(),StandardScaler(),reg)

def poisson_win(home,away):
    h=np.maximum(home,1e-6);a=np.maximum(away,1e-6)
    return skellam.sf(0,h,a)+.5*skellam.pmf(0,h,a)

def fit_platt(prior):
    d=prior[prior.actual_home_score.ne(prior.actual_away_score)].copy()
    if len(d)<100 or d.actual_home_score.gt(d.actual_away_score).nunique()<2:raise ValueError('Insufficient prior independent calibration games')
    x=np.log(np.clip(d.home_win_probability,1e-6,1-1e-6)/(1-np.clip(d.home_win_probability,1e-6,1-1e-6))).to_numpy().reshape(-1,1)
    return make_pipeline(StandardScaler(),LogisticRegression(C=1.,max_iter=500,random_state=20261027)).fit(x,d.actual_home_score.gt(d.actual_away_score))

def calibrated_probability(model,p):
    p=np.clip(p,1e-6,1-1e-6)
    return model.predict_proba(np.log(p/(1-p)).reshape(-1,1))[:,1]

def metrics(d):
    h=d.pred_home_score.to_numpy();a=d.pred_away_score.to_numpy();yh=d.actual_home_score.to_numpy();ya=d.actual_away_score.to_numpy()
    # The published Week7 benchmark uses point-score differences/sums. The
    # legacy simulation's sample means are distinct diagnostics, not substitutes.
    margin=h-a
    total=h+a
    non_tie=yh!=ya;y=(yh>ya)[non_tie].astype(float);p=d.home_win_probability.to_numpy()[non_tie]
    if not len(d) or not np.isfinite(np.r_[h,a,yh,ya,p]).all():raise ValueError('Finite paired predictions/labels required')
    from nfl2026.markets.value_tracking import calibration_metrics
    prob=calibration_metrics(p,y) if len(y) else {}
    return dict(games=len(d),non_tied_games=int(non_tie.sum()),team_score_mae=float((np.abs(h-yh).sum()+np.abs(a-ya).sum())/(2*len(d))),
        team_score_bias=float(np.r_[h-yh,a-ya].mean()),margin_mae=float(np.abs(margin-yh+ya).mean()),total_mae=float(np.abs(total-yh-ya).mean()),
        margin_bias=float((margin-yh+ya).mean()),total_bias=float((total-yh-ya).mean()),winner_correct=int(((p>=.5)==y).sum()),winner_accuracy=float(((p>=.5)==y).mean()),
        win_brier=prob.get('brier'),win_log_loss=prob.get('log_loss'),win_ece=prob.get('ece'))

def paired_uncertainty(candidate,reference,block='week',repeats=2000):
    c=candidate.set_index('game_id');r=reference.set_index('game_id')
    if set(c.index)!=set(r.index) or not c.index.is_unique or not r.index.is_unique:raise ValueError('Exact paired fixture population required')
    r=r.loc[c.index];n=len(c)
    cscore=(np.abs(c.pred_home_score-c.actual_home_score)+np.abs(c.pred_away_score-c.actual_away_score))/2
    rscore=(np.abs(r.pred_home_score-r.actual_home_score)+np.abs(r.pred_away_score-r.actual_away_score))/2
    groups=c.season.astype(str)+'_'+c.week.astype(str) if block=='week' else pd.Series(c.index,index=c.index)
    values=pd.DataFrame({'delta':cscore-rscore,'block':groups}).groupby('block').delta.agg(['sum','count'])
    result=dict(games=n,blocks=len(values),block=block,mae_delta=float((cscore-rscore).mean()),ci95=None)
    if len(values)>1:
        rng=np.random.default_rng(20261027);idx=rng.integers(0,len(values),(repeats,len(values)))
        means=values['sum'].to_numpy()[idx].sum(axis=1)/values['count'].to_numpy()[idx].sum(axis=1)
        result['ci95']=np.quantile(means,[.025,.975]).tolist()
    return result

def strict_timed_context(records,fixtures,values):
    """Future source contract for weather, venue/travel and expected-QB evidence.

    Never substitutes observed game weather or a postgame starting QB.
    """
    if records.duplicated('game_id').any():raise ValueError('Ambiguous context records')
    joined=fixtures[['game_id','kickoff_utc','home_team','away_team']].merge(records,on='game_id',how='left',suffixes=('_fixture',''),validate='one_to_one')
    result=[]
    for r in joined.to_dict('records'):
        try:
            kick=pd.Timestamp(r['kickoff_utc_fixture'] if 'kickoff_utc_fixture' in r else r['kickoff_utc'])
            if 'kickoff_utc_fixture' in r and pd.Timestamp(r['kickoff_utc'])!=kick:raise ValueError('Context kickoff mismatch')
            issued=pd.Timestamp(r['issued_at_utc']);known=pd.Timestamp(r['known_at_utc'])
            cutoff=pd.Timestamp(r.get('prediction_cutoff_utc',kick-pd.Timedelta(minutes=1)))
            cutoff=min(cutoff,kick-pd.Timedelta(minutes=1))
            if any(t.tzinfo is None or pd.isna(t) for t in [kick,issued,known,cutoff]) or not issued<=known<=cutoff:raise ValueError('Post-cutoff/unproved context clock')
            if any(r.get(k)!=r.get(k+'_fixture') for k in ['home_team','away_team']):raise ValueError('Context fixture sides mismatch')
            if r.get('source_type') not in {'PREGAME_FORECAST','DATED_SCHEDULE','DATED_EXPECTED_QB'}:raise ValueError('Unsupported observed/postgame context')
            if not r.get('source_sha256') or len(r['source_sha256'])!=64:raise ValueError('Missing raw context identity')
            result.append(dict(game_id=r['game_id'],accepted=True,**{k:r.get(k) for k in values}))
        except (KeyError,ValueError,TypeError):result.append(dict(game_id=r['game_id'],accepted=False,**{k:None for k in values}))
    return pd.DataFrame(result)

def great_circle_km(origin,destination):
    lat1,lon1=map(float,origin);lat2,lon2=map(float,destination)
    if not np.isfinite([lat1,lon1,lat2,lon2]).all() or not all(-90<=v<=90 for v in [lat1,lat2]) or not all(-180<=v<=180 for v in [lon1,lon2]):
        raise ValueError('Invalid dated venue coordinates')
    p1,p2=map(math.radians,[lat1,lat2]);dp=p2-p1;dl=math.radians(lon2-lon1)
    hav=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 6371.0088*2*math.asin(math.sqrt(min(1.,max(0.,hav))))

def nominal_travel(origin,destination,origin_zone,destination_zone,kickoff):
    """Dated base-to-venue proxy only; never claims actual itinerary/departure."""
    from zoneinfo import ZoneInfo
    point=pd.Timestamp(kickoff)
    if point.tzinfo is None:raise ValueError('UTC fixture clock required')
    return dict(nominal_distance_km=great_circle_km(origin,destination),
        timezone_change_hours=(point.tz_convert(ZoneInfo(destination_zone)).utcoffset()-point.tz_convert(ZoneInfo(origin_zone)).utcoffset()).total_seconds()/3600,
        actual_itinerary_known=False)
