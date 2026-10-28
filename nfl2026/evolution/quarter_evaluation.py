"""Chronological research evaluation; no production registration or forecast writes."""
import json
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import log_loss
from threadpoolctl import threadpool_limits
from nfl2026.evolution.quarters import historical_pbp,quarter_labels,fit_tactics,coarse_state,simulate,STATE
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file


def quarter_metrics(g):
    result={'games':len(g)}
    errors=[]
    for side in ['home','away']:
        e=np.column_stack([g[f'pred_{side}_q{q}']-g[f'{side}_q{q}'] for q in range(1,5)])
        errors.append(e)
        for q in range(4):result[f'{side}_q{q+1}_mae']=float(np.abs(e[:,q]).mean())
    a,b=errors
    result.update(quarter_team_mae=float(np.abs(np.concatenate(errors)).mean()),
        first_half_team_mae=float(np.abs(np.concatenate([e[:,:2].sum(axis=1) for e in errors])).mean()),
        second_half_team_mae=float(np.abs(np.concatenate([e[:,2:].sum(axis=1) for e in errors])).mean()),
        quarter_total_mae=float(np.abs(a+b).mean()),first_half_total_mae=float(np.abs((a+b)[:,:2].sum(axis=1)).mean()),
        final_team_mae=float(np.abs(np.concatenate([g[f'pred_{s}_final']-g[f'{s}_score'] for s in ['home','away']])).mean()))
    y=g.home_score>g.away_score;valid=g.home_score!=g.away_score;p=g.home_win_probability.clip(1e-6,1-1e-6)
    result['win_brier']=float(((p[valid]-y[valid])**2).mean())
    result['win_log_loss']=float(-(y[valid]*np.log(p[valid])+(1-y[valid])*np.log(1-p[valid])).mean())
    return result


def evaluate(root,out,draws=256):
    from scipy.special import ndtr
    from nfl2026.evolution.audit import RUN
    pbp,hashes=historical_pbp(root);labels=quarter_labels(pbp)
    games=pd.read_csv(root/'artifacts/canonical/canonical_games.csv',low_memory=False)
    labels=labels.merge(games[['game_id','home_score','away_score']],on='game_id',validate='one_to_one')
    for s in ['home','away']:
        total=labels[[f'{s}_q{q}' for q in range(1,5)]+[s+'_ot']].sum(axis=1)
        if not np.allclose(total,labels[s+'_score']):raise ValueError('Quarter PBP labels do not reconcile to final scores')
    labels.to_csv(out/'historical_quarter_labels.csv',index=False)
    incumbent=pd.read_csv(root/'artifacts/backtests/phase2_team_predictions.csv')
    incumbent=incumbent[incumbent.model.eq('extra_trees')]
    inputs=pd.read_csv(root/'artifacts/phase2/team_advanced_matrix.csv')
    columns=['home_pregame_plays','away_pregame_plays','home_pregame_drives','away_pregame_drives','home_pregame_points_for','away_pregame_points_for','home_pregame_neutral_pass_rate','away_pregame_neutral_pass_rate']
    data=incumbent.merge(labels.drop(columns=['home_team','away_team']),on=['game_id','season'],validate='one_to_one').merge(inputs[['game_id']+columns],on='game_id',validate='one_to_one')
    allrows=[];decisions=[];sim_failures=[];models={}
    for season in [2024,2025]:
        print('Quarter research chronological holdout',season,flush=True)
        training_pbp=pbp[pbp.season.lt(season)]
        cached=out/f'quarter_tactics_train_through_{season-1}.joblib'
        with threadpool_limits(2):model=joblib.load(cached) if cached.exists() else fit_tactics(training_pbp)
        models[season]=model
        if not cached.exists():joblib.dump(model,cached,compress=3)
        held=pbp[pbp.season.eq(season)&pbp.qtr.between(1,4)]
        fourth=held[held.down.eq(4)&held.play_type.isin(['run','pass','punt','field_goal'])&held.qb_kneel.ne(1)].dropna(subset=STATE)
        y=np.select([fourth.play_type.eq('punt'),fourth.play_type.eq('field_goal')],[0,1],default=2)
        with threadpool_limits(2):p=model.classifier.predict_proba(coarse_state(fourth))[:,[list(model.classifier.classes_).index(c) for c in ['PUNT','FG','GO']]]
        for name,prob in [('historical_coach_frequency',np.tile(model.action_prior,(len(y),1))),('conditional_coach_model',p)]:
            decisions.append(dict(season=season,decision='fourth_down',candidate=name,rows=len(y),log_loss=float(log_loss(y,prob,labels=[0,1,2])),brier=float(np.square(prob-np.eye(3)[y]).sum(axis=1).mean())))
        tries=held[held.extra_point_attempt.eq(1)|held.two_point_attempt.eq(1)]
        pp=np.array([model.two_point_choice[f'{int(r.game_seconds_remaining<=600)}:{int(r.score_differential<0)}'] for r in tries.itertuples()])
        decisions.append(dict(season=season,decision='two_point_choice',candidate='past_conditional_coach',rows=len(tries),brier=float(np.square(pp-tries.two_point_attempt.eq(1)).mean()),observed_rate=float(tries.two_point_attempt.eq(1).mean()),predicted_rate=float(pp.mean())))
        plays=held[held.play_type.isin(['run','pass'])&held.qb_kneel.ne(1)]
        pp=np.array([model.pass_by_state[f'{int(r.game_seconds_remaining<=600)}:{int(r.score_differential<0)}'] for r in plays.itertuples()])
        decisions.append(dict(season=season,decision='run_pass_choice',candidate='past_conditional_coach',rows=len(plays),brier=float(np.square(pp-plays.pass_attempt.eq(1)).mean()),observed_rate=float(plays.pass_attempt.eq(1).mean()),predicted_rate=float(pp.mean())))
        for label,mask,field,positive,pred in [
            ('field_goal_make',held.field_goal_attempt.eq(1)&held.kick_distance.notna(), 'field_goal_result','made',None),
            ('PAT_make',held.extra_point_attempt.eq(1),'extra_point_result','good',model.pat_success),
            ('two_point_make',held.two_point_attempt.eq(1),'two_point_conv_result','success',model.two_point_success)]:
            b=held[mask];target=b[field].eq(positive).to_numpy()
            pp=np.array([model.fg_make[min(8,int(v//10))] for v in b.kick_distance]) if pred is None else np.full(len(b),pred)
            decisions.append(dict(season=season,decision=label,candidate='past_only_smoothed',rows=len(b),brier=float(np.square(pp-target).mean()),observed_rate=float(target.mean()),predicted_rate=float(pp.mean())))
        train=data[data.season.lt(season)];test=data[data.season.eq(season)].sort_values('game_id')
        prior=labels[labels.season.lt(season)];shares={};estimators={}
        for side in ['home','away']:
            totals=prior[[f'{side}_q{q}' for q in range(1,5)]+[side+'_ot']].sum().to_numpy();shares[side]=totals/totals.sum()
            cols=columns+['pred_home_score','pred_away_score']
            target=train[[f'{side}_q{q}' for q in range(1,5)]].to_numpy()/train[side+'_score'].replace(0,np.nan).to_numpy()[:,None]
            valid=np.isfinite(target).all(axis=1)
            estimator=make_pipeline(SimpleImputer(),StandardScaler(),Ridge(alpha=200));estimator.fit(train.loc[valid,cols],target[valid]);estimators[side]=estimator
        for i,r in enumerate(test.itertuples()):
            expected=np.array([r.pred_home_score,r.pred_away_score]);base=r._asdict()
            for name in ['historical_allocation','pace_strength_allocation','conditional_drive']:
                b={**base,'candidate':name,'trained_max_season':season-1}
                if name=='conditional_drive':
                    try:
                        sim=simulate(model,expected,(r.home_team,r.away_team),draws=draws,seed=season*1000+i)
                    except ValueError as e:
                        sim_failures.append({'game_id':r.game_id,'reason':str(e)});continue
                    mean=sim['mean_quarters'];overtime=sim['mean_ot'];b['effective_paths']=sim['effective_paths'];b['home_win_probability']=sim['home_win_probability']
                else:
                    mean=[];overtime=[];x=test[test.game_id.eq(r.game_id)]
                    for j,side in enumerate(['home','away']):
                        share=shares[side];overtime.append(expected[j]*share[4])
                        allocation=share[:4]/share[:4].sum() if name=='historical_allocation' else np.maximum(estimators[side].predict(x[cols])[0],.001)
                        allocation/=allocation.sum();mean.append(allocation*(expected[j]-overtime[-1]))
                    mean=np.array(mean);overtime=np.array(overtime);b['home_win_probability']=float(ndtr((expected[0]-expected[1])/14))
                if not np.allclose(mean.sum(axis=1)+overtime,expected,atol=1e-5):raise ValueError('Quarter expectation anchor mismatch')
                for j,side in enumerate(['home','away']):
                    for q in range(4):b[f'pred_{side}_q{q+1}']=float(mean[j,q])
                    b[f'pred_{side}_ot']=float(overtime[j]);b[f'pred_{side}_final']=float(expected[j])
                allrows.append(b)
        print('Completed',season,len(test),'games',flush=True)
    rows=pd.DataFrame(allrows);rows.to_csv(out/'quarter_backtest_predictions.csv',index=False)
    # Compare on the intersection, including any unsupported-anchor failures explicitly.
    complete=set.intersection(*[set(g.game_id) for _,g in rows.groupby('candidate')]);matched=rows[rows.game_id.isin(complete)]
    pd.DataFrame([dict(season=s,candidate=c,**quarter_metrics(g)) for (s,c),g in matched.groupby(['season','candidate'])]).to_csv(out/'quarter_backtest_metrics.csv',index=False)
    pd.DataFrame(decisions).to_csv(out/'tactical_calibration.csv',index=False)
    print('Preparing research-only Week 7 quarter expectations',flush=True)
    # This is an independently dated research archive, never an official replacement.
    latest_path=out/'quarter_tactics_train_through_2025.joblib'
    with threadpool_limits(2):latest=joblib.load(latest_path) if latest_path.exists() else fit_tactics(pbp)
    if not latest_path.exists():joblib.dump(latest,latest_path,compress=3)
    official=pd.read_csv(root/'predictions/2026/week_07'/RUN/'games.csv');prospective=[]
    for i,r in enumerate(official.itertuples()):
        sim=simulate(latest,[r.home_expected_points,r.away_expected_points],(r.home_team,r.away_team),draws=1024,seed=7000+i)
        for j,side in enumerate(['home','away']):
            prospective.append(dict(game_id=r.game_id,team=getattr(r,side+'_team'),kickoff_utc=r.kickoff_utc,
                **{f'expected_q{q+1}':float(sim['mean_quarters'][j,q]) for q in range(4)},expected_ot=float(sim['mean_ot'][j]),
                expected_final=float(sim['mean_quarters'][j].sum()+sim['mean_ot'][j]),effective_paths=sim['effective_paths'],status='EXPERIMENTAL_RESEARCH_ONLY'))
    pd.DataFrame(prospective).to_csv(out/'week7_quarter_research_only.csv',index=False)
    json_write(out/'quarter_engine_v1.json',{'version':'coarse-drive-anchored-v1','production_ready':False,'frontend_enabled':False,
        'historical_games_label_reconciled':len(labels),'matched_evaluation_games':len(complete),'draws_per_backtest':draws,'simulation_failures':sim_failures,
        'source_sha256':hashes,'quarter_plus_OT_equals_final':True,'expected_final_anchor':'exponential reweighting of whole paired paths, not independent quarter edits',
        'QB_effect':'indirectly through unchanged official team-point anchors only; no new certified QB tactical input',
        'optimal_decisions':'NOT_IMPLEMENTED; empirical coaching behaviour is separate from nfl4th optimal WP recommendations',
        'blockers':sim['limitations']+['full play-level down/distance transitions not implemented','uncertainty and 2026 OT-rule certification incomplete','score reweighting can have low effective sample size']})
