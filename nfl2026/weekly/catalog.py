"""Evaluate existing frozen research only; never fit or register a model."""
from pathlib import Path
import json,hashlib
import numpy as np
import pandas as pd
from nfl2026.weekly.metrics import KEYS,player_metrics,quarter_metrics
from nfl2026.research.frozen_evaluation import verify_freeze,require_pregame_rows
from nfl2026.utilities.io import sha256_file

def point_comparison(frame,official,games,h,a):
    f=frame[np.isfinite(frame[h])&np.isfinite(frame[a])].copy()
    if f.game_id.duplicated().any():raise ValueError('Duplicated research fixture')
    actual=games[['game_id','home_score','away_score']];d=f.merge(actual,on='game_id',validate='one_to_one')
    incumbent=official[official.game_id.isin(d.game_id)].merge(actual,on='game_id',validate='one_to_one')
    def measure(d,h,a):
        he=d[h]-d.home_score;ae=d[a]-d.away_score
        return dict(fixtures=len(d),score_mae=float((he.abs().sum()+ae.abs().sum())/(2*len(d))),margin_mae=float((he-ae).abs().mean()),total_mae=float((he+ae).abs().mean()),home_bias=float(he.mean()),away_bias=float(ae.mean()),probability_metrics='UNAVAILABLE_NO_CERTIFIED_PROBABILITY_MAPPING')
    return dict(challenger=measure(d,h,a),official_same_fixtures=measure(incumbent,'home_expected_points','away_expected_points'))

def evaluate_research(root,official_players,official_games,actuals,games,quarters):
    root=Path(root);results=[];sources={};tables={};v1=root/'artifacts/experimental/2026/week_07/week7-experimental-v1-20261022T172313Z';v2=root/'artifacts/experimental/2026/week_07/week7-experimental-v2-20261022T180347Z'
    for folder in [v1,v2]:
        verify_freeze(folder,games.kickoff_utc.min());sources[str((folder/'freeze_manifest.json').relative_to(root))]=sha256_file(folder/'freeze_manifest.json')
    scores=pd.read_csv(v1/'scores.csv');require_pregame_rows(scores,'created_at_utc')
    for method,h,a in [('V1_QB_REPAIR','qb_home_score','qb_away_score'),('V1_FORM_EW8','form_home_score','form_away_score')]:
        comparison=point_comparison(scores,official_games,games,h,a);results.append(dict(model=method,kind='PROSPECTIVE_FROZEN_RESEARCH',origin_id=v1.name,target='TEAM_SCORES',status='HOLD',**comparison))
    scores2=pd.read_csv(v2/'scores.csv')
    if not np.allclose(scores[['form_home_score','form_away_score']],scores2[['form_home_score','form_away_score']],equal_nan=True):raise ValueError('Copied V1/V2 source origin differs')
    results.append(dict(model='V2_COPIED_QB_FORM',kind='ORIGIN_ALIAS',alias_of=v1.name,status='NOT_COUNTED_AGAIN',reason='V1 numeric origins reused; stale market-implied score columns are not independent AI forecasts.'))
    passing=pd.read_csv(v1/'passing.csv');passing=passing[passing.state.eq('READY')].copy();require_pregame_rows(passing,'created_at_utc')
    for name,column,target in [('V1_PASSING_YARDS','expected_passing_yards','passing_yards'),('V1_PASSING_ATTEMPTS','expected_workload','attempts')]:
        pred=passing[KEYS+['expected_passing_yards','expected_workload']].merge(official_players[KEYS+['position','role_status']],on=KEYS,how='left',validate='one_to_one')
        pred=pred.rename(columns={'expected_passing_yards':'expected_pass_yards','expected_workload':'expected_pass_attempts'})
        metrics,_,_=player_metrics(pred,actuals);chosen=metrics[(metrics.segment=='ALL_FROZEN')&metrics.target.eq(target)].iloc[0].dropna().to_dict()
        results.append(dict(model=name,kind='PROSPECTIVE_FROZEN_RESEARCH',origin_id=v1.name,promotion_decision='HOLD',**chosen))
    fantasy=pd.read_csv(v2/'fantasy.csv');fantasy=fantasy[fantasy.state.eq('RESEARCH_READY')].copy();require_pregame_rows(fantasy,'created_at_utc')
    for name,target in [('QB_passing','passing_yards'),('QB_rushing','rushing_yards'),('RB_rushing','rushing_yards'),('WR_receiving','receiving_yards'),('TE_receiving','receiving_yards'),('WR_targets','targets')]:
        selected=fantasy[fantasy.target.eq(name)].copy()
        if not len(selected):continue
        field={'passing_yards':'expected_pass_yards','rushing_yards':'expected_rush_yards','receiving_yards':'expected_receiving_yards','targets':'expected_targets'}[target]
        pred=selected[KEYS+['research_value']].rename(columns={'research_value':field}).merge(official_players[KEYS+['position','role_status']],on=KEYS,how='left',validate='one_to_one')
        metrics,_,_=player_metrics(pred,actuals);chosen=metrics[metrics.segment.eq('ALL_FROZEN')&metrics.target.eq(target)].iloc[0].dropna().to_dict()
        results.append(dict(model='V2_FANTASY_'+name,kind='PROSPECTIVE_FROZEN_RESEARCH',origin_id=v2.name,promotion_decision='HOLD',**chosen))
    quarter_origins={}
    for folder in [v1,v2]:
        q=pd.read_csv(folder/'quarters.csv');q=q[q.state.isin(['READY','RESEARCH_READY'])].copy();require_pregame_rows(q,'created_at_utc')
        canonical=q[['game_id','team','q1','q2','q3','q4']].sort_values(['game_id','team']).to_csv(index=False,float_format='%.10f')
        identity=hashlib.sha256(canonical.encode()).hexdigest();alias=quarter_origins.get(identity);quarter_origins[identity]=folder.name
        if alias:results.append(dict(model=folder.name+'_QUARTERS',kind='ORIGIN_ALIAS',alias_of=alias,status='NOT_COUNTED_AGAIN',target='QUARTERS'));continue
        pred=q.rename(columns={c:'pred_'+c for c in ['q1','q2','q3','q4']});m=quarter_metrics(pred,quarters)
        results.append(dict(model=folder.name+'_QUARTERS',kind='PROSPECTIVE_FROZEN_RESEARCH',origin_id=folder.name,status='HOLD_OLD_FINAL_OT_ALLOCATION_NOT_PRODUCTION',target='QUARTERS',**m))
    from nfl2026.phase7.shadow import verify_shadow
    shadow=root/'artifacts/research/phase7/shadows/2026/week_07/shadow-20261021T210829042284Z-extra_trees';manifest=verify_shadow(shadow)
    pred=pd.read_csv(shadow/'predictions.csv');require_pregame_rows(pred.assign(created_at_utc=manifest['created_at_utc']),'created_at_utc')
    pred=pred.merge(official_players[KEYS+['role_status','expected_carries','expected_targets','expected_rush_yards','expected_receiving_yards']],on=KEYS,validate='one_to_one',suffixes=('','_incumbent'))
    replacements={'pred_carries':'expected_carries','pred_targets':'expected_targets','pred_rushing_yards':'expected_rush_yards','pred_receiving_yards':'expected_receiving_yards','pred_receptions':'expected_receptions','pred_anytime_offensive_td':'reconciled_td_probability'}
    research=pred[KEYS+['position','role_status',*replacements]].rename(columns=replacements)
    metrics,j,bins=player_metrics(research,actuals);tables.update(v4_metrics=metrics,v4_joined=j,v4_calibration=bins)
    results.append(dict(model='V4_FROZEN_EXTRA_TREES',kind='PROSPECTIVE_SHADOW',origin_id=shadow.name,status='HOLD_INDEPENDENT_TARGET_GATES',frozen_rows=len(pred),matched_identities=int(j.label_match.eq('both').sum()),unknown_identities=int(j.label_match.ne('both').sum()),target_metrics_file='v4_metrics.csv'))
    # Reproduce original opportunity-defined cohorts without postgame selection.
    paired=[]
    for target,field,positions,mask in [('rushing_yards','expected_rush_yards',['QB','RB','FB'],official_players.expected_carries.ge(1)|official_players.expected_rush_yards.ge(5)),('receiving_yards','expected_receiving_yards',['WR','TE','RB','FB'],official_players.expected_targets.ge(1)|official_players.expected_receiving_yards.ge(8))]:
        ids=official_players.loc[mask&official_players.position.isin(positions),KEYS]
        inc=official_players.merge(ids,on=KEYS,validate='one_to_one');candidate=research.merge(ids,on=KEYS,validate='one_to_one')
        a,_,_=player_metrics(inc,actuals);b,_,_=player_metrics(candidate,actuals)
        x=a[a.segment.eq('ALL_FROZEN')&a.target.eq(target)].iloc[0];y=b[b.segment.eq('ALL_FROZEN')&b.target.eq(target)].iloc[0]
        paired.append(dict(target=target,cohort='FROZEN_MEANINGFUL_OPPORTUNITY',incumbent_mae=float(x.mae),v4_mae=float(y.mae),evaluated_rows=int(x.evaluated_rows),frozen_rows=int(x.frozen_rows)))
    tables['v4_original_cohort_reproduction']=pd.DataFrame(paired)
    sources[str((shadow/'manifest.json').relative_to(root))]=sha256_file(shadow/'manifest.json')
    for folder in [v1,v2]:
        for p in folder.glob('*.csv'):sources[str(p.relative_to(root))]=sha256_file(p)
    sources[str((shadow/'predictions.csv').relative_to(root))]=sha256_file(shadow/'predictions.csv')
    # Later retrospective fits are explicitly catalogued, never new prospective samples.
    results.append(dict(model='PHASE3_PASSING_CHALLENGER',kind='RETROSPECTIVE_AFTER_WEEK7',status='HOLD_NOT_PROSPECTIVE',report='reports/passing_phase3_20261027T155703Z/validated/week7_retrospective_metrics.csv'))
    return results,tables,sources
