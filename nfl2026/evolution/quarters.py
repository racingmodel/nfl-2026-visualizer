"""Research-only quarter baselines and conditional drive-state simulation.

Observed coaching behaviour is learned separately from optimal decisions. This
is a coarse drive engine, not nfl4th or a certified play-level/OT rule engine.
"""
from dataclasses import dataclass
import json
import numpy as np
import pandas as pd
from scipy.optimize import root as solve_root
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import log_loss
from threadpoolctl import threadpool_limits
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file

STATE=['yardline_100','ydstogo','score_differential','game_seconds_remaining','qtr','posteam_timeouts_remaining']


def historical_pbp(root):
    import pyarrow.parquet as pq
    wanted=['game_id','play_id','season','season_type','week','home_team','away_team','posteam','defteam','qtr',
        'total_home_score','total_away_score','game_seconds_remaining','quarter_seconds_remaining','yardline_100','ydstogo','down',
        'score_differential','posteam_timeouts_remaining','defteam_timeouts_remaining','drive','play_type','touchdown','td_team',
        'interception','fumble_lost','field_goal_attempt','field_goal_result','kick_distance','extra_point_attempt','extra_point_result',
        'two_point_attempt','two_point_conv_result','pass_attempt','rush_attempt','qb_kneel','timeout','safety']
    parts=[];hashes={}
    for year in [2022,2023,2024,2025]:
        path=root/f'NFL Fantasy/{year} Stats/pbp_{year}.parquet';names=pq.ParquetFile(path).schema.names
        if not set(STATE+['drive','total_home_score','total_away_score']).issubset(names):raise ValueError('PBP state schema missing')
        d=pq.read_table(path,columns=[c for c in wanted if c in names]).to_pandas();parts.append(d[d.season_type.eq('REG')]);hashes[str(path.relative_to(root))]=sha256_file(path)
    return pd.concat(parts,ignore_index=True),hashes


def quarter_labels(pbp):
    if pbp.duplicated(['game_id','play_id']).any():raise ValueError('Duplicate PBP play')
    rows=[]
    for gid,g in pbp.groupby('game_id',sort=False):
        end=g.groupby('qtr')[['total_home_score','total_away_score']].max().reindex(range(1,6)).ffill().fillna(0)
        delta=end.diff().fillna(end.iloc[0])
        if (delta<0).any().any():continue
        r=dict(game_id=gid,season=int(g.season.iloc[0]),home_team=g.home_team.iloc[0],away_team=g.away_team.iloc[0])
        for side in ['home','away']:
            for q in range(1,5):r[f'{side}_q{q}']=float(delta.loc[q,'total_'+side+'_score'])
            r[side+'_ot']=float(delta.loc[5,'total_'+side+'_score'])
        rows.append(r)
    return pd.DataFrame(rows)


def coarse_state(frame):
    d=frame[STATE].copy().fillna({'yardline_100':50,'ydstogo':10,'score_differential':0,'game_seconds_remaining':1800,'qtr':2,'posteam_timeouts_remaining':0})
    return pd.DataFrame({'field':np.clip((d.yardline_100/10).astype(int),0,9),'distance':np.clip((d.ydstogo/3).astype(int),0,4),
        'margin':np.digitize(d.score_differential,[-9,-1,1,9]),'clock':np.digitize(d.game_seconds_remaining,[120,600]),
        'quarter':np.clip(d.qtr.astype(int)-1,0,3),'timeouts':np.clip(d.posteam_timeouts_remaining.astype(int),0,3)},index=d.index)


@dataclass
class TacticalModel:
    classifier: object
    fourth_probabilities: np.ndarray
    fg_make: np.ndarray
    conversion: np.ndarray
    two_point_choice: dict
    pat_success: float
    two_point_success: float
    pass_by_state: dict
    team_choice: dict
    templates: pd.DataFrame
    overtime_pairs: np.ndarray
    trained_max_season: int
    action_prior: np.ndarray
    max_fg_distance: float


def fit_tactics(pbp):
    d=pbp[pbp.qtr.between(1,4)].copy()
    fourth=d[d.down.eq(4)&d.play_type.isin(['run','pass','punt','field_goal'])&d.qb_kneel.ne(1)].dropna(subset=STATE).copy()
    fourth['action']=np.select([fourth.play_type.eq('punt'),fourth.play_type.eq('field_goal')],['PUNT','FG'],default='GO')
    clf=HistGradientBoostingClassifier(max_iter=70,max_leaf_nodes=9,min_samples_leaf=60,l2_regularization=10,random_state=20261022)
    clf.fit(coarse_state(fourth),fourth.action)
    shape=(10,5,5,3,4,4);grid=np.array(np.unravel_index(np.arange(np.prod(shape)),shape)).T
    probs=clf.predict_proba(pd.DataFrame(grid,columns=['field','distance','margin','clock','quarter','timeouts'])).reshape((*shape,3))
    # Fix class order once, rather than infer a numeric label ordering.
    probs=probs[..., [list(clf.classes_).index(c) for c in ['PUNT','FG','GO']]]
    fg=d[d.field_goal_attempt.eq(1)&d.field_goal_result.isin(['made','missed','blocked'])&d.kick_distance.notna()]
    fg_make=[]
    for b in range(9):
        group=fg[(fg.kick_distance//10).clip(0,8).eq(b)];fg_make.append((group.field_goal_result.eq('made').sum()+2)/(len(group)+4))
    go=fourth[fourth.action.eq('GO')];conversion=[]
    # Actual conversion is derived from next first down or a scoring play within the same drive.
    ordered=pbp.sort_values(['game_id','play_id']).copy();ordered['next_down']=ordered.groupby(['game_id','drive','posteam']).down.shift(-1)
    go=go.merge(ordered[['game_id','play_id','next_down']],on=['game_id','play_id'],validate='one_to_one')
    for b in range(5):
        group=go[(go.ydstogo//3).clip(0,4).eq(b)];conversion.append((int((group.next_down.eq(1)|group.touchdown.eq(1)).sum())+2)/(len(group)+4))
    pat=d[d.extra_point_attempt.eq(1)];two=d[d.two_point_attempt.eq(1)]
    choices={};pass_rates={}
    for late in [False,True]:
        for trailing in [False,True]:
            key=f'{int(late)}:{int(trailing)}';context=d[d.game_seconds_remaining.le(600).eq(late)&d.score_differential.lt(0).eq(trailing)]
            tries=context[context.extra_point_attempt.eq(1)|context.two_point_attempt.eq(1)]
            choices[key]=(tries.two_point_attempt.eq(1).sum()+1)/(len(tries)+20)
            plays=context[context.play_type.isin(['run','pass'])&context.qb_kneel.ne(1)]
            pass_rates[key]=(plays.pass_attempt.eq(1).sum()+1)/(len(plays)+2)
    templates=[]
    plays=d[d.play_type.isin(['run','pass','punt','field_goal'])&d.posteam.notna()].sort_values(['game_id','play_id'])
    for (_,_,team),g in plays.groupby(['game_id','drive','posteam'],sort=False):
        first=g.iloc[0];last=g.iloc[-1]
        if pd.isna(first.yardline_100) or pd.isna(last.yardline_100):continue
        duration=first.game_seconds_remaining-last.game_seconds_remaining+5
        if not 1<=duration<=900:continue
        templates.append(dict(team=team,seconds=float(duration),gain=float(first.yardline_100-last.yardline_100),
            td=bool((g.touchdown.eq(1)&g.td_team.eq(team)).any()),turnover=bool((g.interception.eq(1)|g.fumble_lost.eq(1)).any()),
            defensive_td=bool((g.touchdown.eq(1)&g.td_team.notna()&g.td_team.ne(team)).any()),
            fourth=bool(last.down==4),start_field=float(first.yardline_100),distance=float(last.ydstogo) if pd.notna(last.ydstogo) else 10,
            pass_heavy=float(g.pass_attempt.eq(1).mean())>.5,late=bool(first.game_seconds_remaining<=600),trailing=bool(first.score_differential<0)))
    ot=quarter_labels(pbp);ot=ot[(ot.home_ot+ot.away_ot)>0][['home_ot','away_ot']].to_numpy()
    team_choice={team:((g.action.value_counts().reindex(['PUNT','FG','GO'],fill_value=0)+20)/(len(g)+60)).to_numpy() for team,g in fourth.groupby('posteam')}
    return TacticalModel(clf,probs,np.array(fg_make),np.array(conversion),choices,
        (pat.extra_point_result.eq('good').sum()+9)/(len(pat)+10),
        (two.two_point_conv_result.eq('success').sum()+1)/(len(two)+2),pass_rates,team_choice,pd.DataFrame(templates),ot,int(pbp.season.max()),
        (fourth.action.value_counts().reindex(['PUNT','FG','GO'],fill_value=0)/len(fourth)).to_numpy(),float(fg.kick_distance.max()))


def tactical_probabilities(model,states,team=None):
    bins=coarse_state(states).to_numpy(dtype=int);p=model.fourth_probabilities[tuple(bins.T)].copy()
    if team in model.team_choice:p*=model.team_choice[team]/model.action_prior;p/=p.sum(axis=1,keepdims=True)
    # Physical feasibility, independent of a statistical recommendation.
    impossible=states.yardline_100.to_numpy()+17>model.max_fg_distance;p[impossible,1]=0;p/=p.sum(axis=1,keepdims=True)
    return p


def conversion_choice_probability(model,time_remaining,margin_after_touchdown):
    return model.two_point_choice[f'{int(time_remaining<=600)}:{int(margin_after_touchdown<0)}']


def anchor_paths(quarters,ot,expected):
    """Reweight whole legal paired paths; never edit quarter/player projections."""
    finals=quarters.sum(axis=2)+ot;target=np.asarray(expected,dtype=float)
    if np.any(target<0):raise ValueError('Negative authoritative score mean')
    def weights(theta):
        logits=(finals-target)@theta;v=np.exp(logits-logits.max());return v/v.sum()
    solved=solve_root(lambda theta:weights(theta)@finals-target,np.zeros(2))
    w=weights(solved.x);error=float(np.max(np.abs(w@finals-target)))
    if not solved.success or error>1e-5:raise ValueError('Authoritative score means not supported by simulated paths')
    return w,1/float(np.sum(w*w))


def simulate(model,expected,teams=('HOME','AWAY'),draws=512,seed=0):
    if draws<100:raise ValueError('Insufficient simulation paths')
    rng=np.random.default_rng(seed);scores=np.zeros((draws,2,4),dtype=int);ot=np.zeros((draws,2),dtype=int)
    time=np.full(draws,3600.);possession=rng.integers(0,2,draws);opening=possession.copy();field=np.full(draws,75.)
    timeouts=np.full((draws,2),3,dtype=int);templates=model.templates;events=[]
    starts=templates.start_field.to_numpy();field=rng.choice(starts,draws)
    if not hasattr(model,'_pools'):
        model._pools={}
        for late in [False,True]:
            for trailing in [False,True]:
                for passing in [False,True]:
                    subset=templates[templates.late.eq(late)&templates.trailing.eq(trailing)&templates.pass_heavy.eq(passing)]
                    if len(subset)<20:subset=templates
                    model._pools[late,trailing,passing]=list(subset.itertuples(index=False,name='Drive'))
    pools=model._pools
    for turn in range(80):
        live=np.flatnonzero(time>0)
        if not len(live):break
        for side in [0,1]:
            active=live[possession[live]==side]
            if not len(active):continue
            margin=scores[active,side].sum(axis=1)-scores[active,1-side].sum(axis=1)
            sampled=[]
            for late,trailing in [(False,False),(False,True),(True,False),(True,True)]:
                indexes=active[(time[active]<=600)==late];indexes=indexes[(scores[indexes,side].sum(axis=1)-scores[indexes,1-side].sum(axis=1)<0)==trailing]
                if not len(indexes):continue
                pass_rate=model.pass_by_state[f'{int(late)}:{int(trailing)}']
                for i in indexes:
                    pool=pools[late,trailing,bool(rng.random()<pass_rate)]
                    sampled.append((i,pool[rng.integers(len(pool))]))
            for i,r in sampled:
                available=time[i]-1800 if time[i]>1800 else time[i]
                duration=float(r.seconds);fits=duration<=available;duration=min(duration,available)
                quarter=int(np.clip((3600-time[i]+max(0,duration-1))//900,0,3));points=0;other_points=0
                lead=int(scores[i,side].sum()-scores[i,1-side].sum());late=time[i]<600
                # Coarse conservative kneel policy, explicitly not an optimal WP rule.
                if 0<time[i]<=40 and lead>0 and timeouts[i,1-side]==0:
                    time[i]=0;continue
                if fits and r.td:
                    two=rng.random()<conversion_choice_probability(model,time[i]-duration,lead+6)
                    points=6+(2*int(rng.random()<model.two_point_success) if two else int(rng.random()<model.pat_success))
                elif fits and r.defensive_td:other_points=6+int(rng.random()<model.pat_success)
                elif fits and not r.turnover and r.fourth:
                    end=float(np.clip(field[i]-r.gain,1,99))
                    bins=(min(9,int(end//10)),min(4,int(r.distance//3)),int(np.digitize(lead,[-9,-1,1,9])),int(np.digitize(time[i]-duration,[120,600])),quarter,int(timeouts[i,side]))
                    p=model.fourth_probabilities[bins].copy()
                    if teams[side] in model.team_choice:p*=model.team_choice[teams[side]]/model.action_prior
                    if end+17>model.max_fg_distance:p[1]=0
                    p/=p.sum();action=rng.choice(3,p=p)
                    if action==1:points=3*int(rng.random()<model.fg_make[min(8,int((end+17)//10))])
                    elif action==2 and rng.random()<model.conversion[min(4,int(r.distance//3))]:
                        # A converted fourth down continues as a separate empirical
                        # drive segment. It consumes real clock on the next turn.
                        field[i]=max(1,end-r.distance);time[i]-=duration
                        if time[i]==1800:possession[i]=1-opening[i];timeouts[i,:]=3;field[i]=rng.choice(starts)
                        continue
                    if late and lead<0 and timeouts[i,side]>0:timeouts[i,side]-=1
                scores[i,side,quarter]+=points;scores[i,1-side,quarter]+=other_points
                time[i]-=duration;possession[i]=1-side;field[i]=rng.choice(starts)
                if time[i]==1800:possession[i]=1-opening[i];timeouts[i,:]=3
                events.append((quarter+1,side,points,lead,late))
    if (time>0).any():raise ValueError('Simulation clock did not complete; increase validated turn budget')
    tied=scores.sum(axis=2)[:,0]==scores.sum(axis=2)[:,1]
    if tied.any() and len(model.overtime_pairs):ot[tied]=model.overtime_pairs[rng.integers(0,len(model.overtime_pairs),tied.sum())].astype(int)
    weights,ess=anchor_paths(scores,ot,expected)
    return {'quarters':scores,'overtime':ot,'finals':scores.sum(axis=2)+ot,'weights':weights,'effective_paths':ess,
        'mean_quarters':np.einsum('i,ijk->jk',weights,scores),'mean_ot':weights@ot,
        'home_win_probability':float(weights@(scores.sum(axis=2)[:,0]+ot[:,0]>scores.sum(axis=2)[:,1]+ot[:,1])),
        'limitations':['coarse empirical drive segments, not full play simulation','empirical start-field distribution; special-teams detail incomplete','timeouts approximate; OT empirical across past rule eras, not a 2026 rule engine','fresh QB/lineup/weather adjustment not certified','coaching action tendencies, not optimal win-probability decisions']}
