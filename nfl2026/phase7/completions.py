"""Complete nominal-roster QB count research, never official inference."""
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits
from nfl2026.phase7.common import json_write
from nfl2026.five_week_review import immutable_research_artifact
from nfl2026.utilities.io import sha256_file


def lag_completions(candidates,history):
    out=candidates.copy();out['lag_completions']=np.nan
    times=pd.to_datetime(history.kickoff_utc,utc=True,format='mixed')
    for pid,indices in out.groupby('player_id').groups.items():
        prior=history[history.player_id.eq(pid)&history.pass_attempts.gt(0)].copy()
        prior['_time']=times.loc[prior.index]
        prior=prior.sort_values('_time')
        for index in indices:
            eligible=prior[prior._time.lt(pd.Timestamp(out.loc[index,'kickoff_utc']))]
            if len(eligible):out.loc[index,'lag_completions']=eligible.tail(8).completions.mean()
    return out


def build_completions_research(settings):
    import pyarrow.parquet as pq
    from nfl2026.phase7.player_v4 import build_research_panel
    from nfl2026.phase7.passing import lag_passing
    research=settings.artifact_root/'research/phase7'
    panel,features=build_research_panel(settings);panel=panel[panel.position.eq('QB')].copy()
    history=pd.read_csv(research/'qb_passing_history.csv')
    extra=[];hashes={}
    for season in (2022,2023,2024,2025):
        path=settings.root/f'NFL Fantasy/{season} Stats/pbp_{season}.parquet'
        p=pq.read_table(path,columns=['game_id','play_id','posteam','passer_player_id','complete_pass','play_type','two_point_attempt','season_type']).to_pandas()
        p=p[p.season_type.eq('REG')]
        if p.duplicated(['game_id','play_id']).any():raise ValueError('Duplicate completion PBP')
        if set(p.game_id)!=set(history[history.season.eq(season)].game_id):raise ValueError('Incomplete completion PBP coverage')
        p=p[p.passer_player_id.notna()&p.posteam.notna()&p.play_type.ne('no_play')&p.two_point_attempt.ne(1)]
        p['completions']=p.complete_pass.eq(1).astype(int)
        extra.append(p.groupby(['game_id','posteam','passer_player_id']).completions.sum().reset_index().rename(columns={'posteam':'team','passer_player_id':'player_id'}))
        hashes[str(path.relative_to(settings.root))]=sha256_file(path)
    history=history.merge(pd.concat(extra),on=['game_id','team','player_id'],validate='one_to_one')
    if (history.completions>history.pass_attempts).any():raise ValueError('Completion target exceeds attempts')
    data=lag_completions(lag_passing(panel,history),history)
    data=data.merge(history[['game_id','team','player_id','completions']],on=['game_id','team','player_id'],how='left',validate='one_to_one')
    data.completions=data.completions.fillna(0)
    features=features+['lag_pass_attempts','lag_passing_yards','lag_passing_tds','passing_games_before','lag_completions']
    attempts=pd.read_csv(research/'qb_passing_oos.csv')
    metrics=[];parts=[]
    with threadpool_limits(2):
        for season in (2023,2024,2025):
            train,test=data[data.season.lt(season)],data[data.season.eq(season)].copy()
            max_train=pd.to_datetime(train.kickoff_utc,utc=True,format='mixed').max()
            if max_train>=pd.to_datetime(test.kickoff_utc,utc=True,format='mixed').min():raise ValueError('Completion fold leakage')
            model=make_pipeline(SimpleImputer(strategy='median'),ExtraTreesRegressor(n_estimators=80,max_depth=6,min_samples_leaf=10,max_features=.75,random_state=20261014,n_jobs=2))
            model.fit(train[features],train.completions)
            test['pred_completions_raw']=model.predict(test[features])
            test=test.merge(attempts[['game_id','team','player_id','pred_pass_attempts']],on=['game_id','team','player_id'],validate='one_to_one')
            if test.pred_pass_attempts.isna().any():raise ValueError('OOS attempt forecasts missing')
            test['pred_completions']=np.minimum(np.maximum(test.pred_completions_raw,0),test.pred_pass_attempts)
            baseline=test.lag_completions.fillna(train.completions.median())
            e=test.pred_completions-test.completions
            metrics.append({'season':season,'target':'completions','rows':len(test),'mae':e.abs().mean(),'rmse':np.sqrt((e**2).mean()),'bias':e.mean(),'lag_baseline_mae':(baseline-test.completions).abs().mean(),'coherent_with_attempts':bool(test.pred_completions.le(test.pred_pass_attempts).all()),'certified':False})
            test['max_training_kickoff_utc']=max_train.isoformat();parts.append(test[['game_id','team','player_id','season','week','kickoff_utc','completions','pred_completions','pred_pass_attempts','max_training_kickoff_utc']])
            if season==2025:
                immutable_research_artifact(research/'models/qb-completions-roster-research-v1.joblib',{'model':model,'features':features},
                    {'training_seasons':[2022,2023,2024],'max_training_kickoff_utc':max_train.isoformat(),'source_hashes':hashes,'production_registered':False,'population':'nominal roster QBs; historical expected starters uncertified'})
    pd.concat(parts).to_csv(research/'qb_completions_oos.csv',index=False)
    pd.DataFrame(metrics).to_csv(settings.report_root/'phase75_completions_metrics.csv',index=False)
    json_write(settings.report_root/'phase75_completions_summary.json',{'state':'RESEARCH_ONLY','rows':sum(len(p) for p in parts),'metrics':metrics,'promotion':False,'interval_calibration':'NOT_CERTIFIED','inputs':features,'training_seasons':[2022,2023,2024],'target':'PBP completed passes, excludes nullified/2PT plays; no-pass QBs zero with complete game coverage','historical_starter_provenance':'UNPROVEN'})
    return metrics
