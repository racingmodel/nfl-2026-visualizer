"""Regulation-only quarter research and an explicit, fail-closed parent contract."""
from __future__ import annotations
from decimal import Decimal,ROUND_FLOOR,ROUND_HALF_UP
import hashlib,json,math,re
import numpy as np
import pandas as pd

Q=['q1','q2','q3','q4']
FEATURES=['own_share_'+q for q in Q]+['opponent_share_'+q for q in Q]+[
    'opening_drive_points_8','own_reg_points_8','opponent_reg_allowed_8','plays_8','pass_epa_8',
    'late_trailing_points_8','rest_days','home','reference_margin']
PBP_COLS=['game_id','play_id','order_sequence','season','season_type','week','home_team','away_team','posteam','qtr',
    'total_home_score','total_away_score','posteam_score_post','defteam_score_post','drive',
    'play_type','game_seconds_remaining','score_differential']

def strict_quarter_labels(pbp,schedule):
    """Explicit quarter presence and final reconciliation; no silent ffill/skip."""
    if pbp.duplicated(['game_id','play_id']).any():raise ValueError('Duplicate PBP identity')
    if pbp.order_sequence.isna().any() or pbp.duplicated(['game_id','order_sequence']).any():raise ValueError('PBP chronological sequence unavailable')
    if schedule.game_id.duplicated().any() or set(pbp.game_id)!=set(schedule.game_id):raise ValueError('Schedule/PBP population mismatch')
    by=schedule.set_index('game_id');rows=[];aux=[]
    for gid,g in pbp.groupby('game_id',sort=False):
        g=g.sort_values('order_sequence').copy();s=by.loc[gid]
        if set(g.home_team.dropna())!={s.home_team} or set(g.away_team.dropna())!={s.away_team}:raise ValueError('Quarter fixture team mismatch')
        if not set([1,2,3,4]).issubset(set(g.qtr.dropna())):raise ValueError('Missing regulation quarter '+gid)
        if g.qtr.dropna().lt(1).any():raise ValueError('Invalid quarter')
        for side,other in [('home','away'),('away','home')]:
            team=s[side+'_team'];opponent=s[other+'_team']
            # Explicit end-of-play possession scores cross-check the running score.
            post=np.where(g.posteam.eq(team),g.posteam_score_post,np.where(g.posteam.eq(opponent),g.defteam_score_post,np.nan))
            g[side+'_after']=np.fmax(g['total_'+side+'_score'].to_numpy(float),post.astype(float))
            if not np.isfinite(g[side+'_after']).all():raise ValueError('Unknown running quarter score '+gid)
            ends=g.groupby('qtr')[side+'_after'].max()
            reg=ends.reindex([1,2,3,4]).to_numpy(float)
            if not np.isfinite(reg).all() or (np.diff(np.r_[0,reg])<0).any():raise ValueError('Contradictory quarter scoreboard '+gid)
            final=float(g[side+'_after'].max());official=float(s[side+'_score'])
            if not np.isfinite(official) or final!=official:raise ValueError('Quarter/final mismatch '+gid)
            ot=final-reg[-1]
            if ot<0 or (ot>0 and not g.qtr.gt(4).any()):raise ValueError('Invalid overtime label '+gid)
            row=dict(game_id=gid,team=team,opponent=opponent,home=int(side=='home'),season=int(s.season),week=int(s.week),kickoff_utc=str(s.kickoff_utc),regulation_points=float(reg[-1]),overtime_points=float(ot),final_points=final)
            row.update(dict(zip(Q,np.diff(np.r_[0,reg]))));rows.append(row)
            # Administrative timeout rows can carry stale 0 running scores and
            # no possession/post-play score. They cannot create/erase points.
            # Use explicit possession post-play rows for these optional proxies.
            trusted=g.posteam.isin([team,opponent])&np.isfinite(post)
            observed=g.loc[trusted,side+'_after']
            delta=pd.Series(0.,index=g.index)
            if len(observed):delta.loc[trusted]=observed.diff().fillna(observed.iloc[0])
            if (delta<0).any():raise ValueError('Possession score rollback needs explicit resolution '+gid)
            administrative_drop=int(((g[side+'_after'].diff()<0)&~trusted).sum())
            offensive=g[g.posteam.eq(team)&g.play_type.isin(['run','pass','field_goal'])&g.drive.notna()]
            opening=np.nan
            if len(offensive):
                first=offensive.drive.iloc[0]
                opening=float(delta[g.drive.eq(first)&g.posteam.eq(team)].sum())
            late=(g.qtr.eq(4)&g.game_seconds_remaining.le(120)&g.posteam.eq(team)&g.score_differential.lt(0))
            aux.append(dict(game_id=gid,team=team,opening_drive_points=opening,late_trailing_points=float(delta[late].sum()),administrative_score_drop_rows=administrative_drop))
    d=pd.DataFrame(rows)
    if d.duplicated(['game_id','team']).any() or len(d)!=2*len(schedule):raise ValueError('Quarter label identity')
    return d,pd.DataFrame(aux)

def allocate_regulation(total,shares):
    """Expected points, not a simulated legal NFL score scenario. No OT input."""
    s=np.asarray(shares,float)
    if not math.isfinite(total) or total<0 or s.shape!=(4,) or not np.isfinite(s).all() or (s<0).any() or s.sum()<=0:
        raise ValueError('Four finite nonnegative regulation shares required')
    s=s/s.sum();a=float(total)*s;a[-1]=float(total)-float(a[:3].sum())
    if a[-1]<-1e-10:raise ValueError('Invalid reconciliation')
    a=np.maximum(0,a)
    if not np.isclose(a.sum(),total,rtol=0,atol=1e-10):raise ValueError('Quarter reconciliation')
    return a

def rounded_quarters(values,total,places=1):
    """Largest remainder display allocation, exact to the displayed REG parent."""
    factor=Decimal(10)**places;target=int((Decimal(str(total))*factor).to_integral_value(rounding=ROUND_HALF_UP))
    raw=[Decimal(str(v))*factor for v in values]
    if len(raw)!=4 or any(not x.is_finite() or x<0 for x in raw) or not np.isclose(sum(map(float,values)),total,atol=1e-8):raise ValueError('Bad display allocation')
    base=[int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw];remaining=target-sum(base)
    if remaining<0 or remaining>4:raise ValueError('Display precision mismatch')
    order=sorted(range(4),key=lambda i:(-(raw[i]-base[i]),i))
    for i in order[:remaining]:base[i]+=1
    return [float(Decimal(x)/factor) for x in base]

def historical_features(labels,aux,context):
    """Clean prior-outcome histories only. 48h embargo is not publication proof."""
    d=labels.copy().reset_index(drop=True);h=labels.merge(aux,on=['game_id','team'],validate='one_to_one')
    h=h.merge(context[['game_id','team','plays','epa']],on=['game_id','team'],how='left',validate='one_to_one')
    h['time']=pd.to_datetime(h.kickoff_utc,utc=True)
    own_index={t:b.sort_values('time') for t,b in h.groupby('team')}
    opp_index={t:b.sort_values('time') for t,b in h.groupby('opponent')}
    for f in FEATURES:
        if f!='home':d[f]=np.nan
    d['reference_reg_parent']=np.nan;d['max_history_kickoff']=None;d['source_publication_certified']=False
    for i,r in d.iterrows():
        cutoff=pd.Timestamp(r.kickoff_utc)-pd.Timedelta(hours=48)
        own=own_index[r.team];own=own[own.time.lt(cutoff)].tail(8)
        opp=opp_index.get(r.opponent,h.iloc[:0]);opp=opp[opp.time.lt(cutoff)].tail(8)
        league=h[h.time.lt(cutoff)&h.home.eq(r.home)]
        base=league[Q].sum().to_numpy()/league.regulation_points.sum() if league.regulation_points.sum()>0 else np.full(4,np.nan)
        for prefix,b in [('own',own),('opponent',opp)]:
            if len(b) and np.isfinite(base).all():
                # Fixed 100-point shrinkage, specified before development scoring.
                shares=(b[Q].sum().to_numpy()+100*base)/(b.regulation_points.sum()+100)
                d.loc[i,[prefix+'_share_'+q for q in Q]]=shares
        if len(own):
            d.loc[i,['opening_drive_points_8','own_reg_points_8','plays_8','pass_epa_8','late_trailing_points_8','rest_days']]=[
                own.opening_drive_points.mean(),own.regulation_points.mean(),own.plays.mean(),own.epa.mean(),own.late_trailing_points.mean(),
                min(30,(pd.Timestamp(r.kickoff_utc)-own.time.iloc[-1]).total_seconds()/86400)]
        if len(opp):d.loc[i,'opponent_reg_allowed_8']=opp.regulation_points.mean()
        if len(own) and len(opp):d.loc[i,'reference_reg_parent']=.5*(own.regulation_points.mean()+opp.regulation_points.mean())
        used=list(own.time)+list(opp.time)
        if used:d.loc[i,'max_history_kickoff']=max(used).isoformat()
    # The margin uses two independently constructed pregame REG reference points.
    opponent=d[['game_id','team','reference_reg_parent']].rename(columns={'team':'opponent','reference_reg_parent':'opponent_reference_reg'})
    d=d.merge(opponent,on=['game_id','opponent'],validate='one_to_one')
    d['reference_margin']=d.reference_reg_parent-d.opponent_reference_reg
    return d

def require_regulation_parent(parent,*,game_id,team,now):
    """Reject the legacy final-score parent; never subtract an OT share here."""
    required=['run_id','freeze_id','model_sha256','feature_sha256','certificate_id','game_id','team','kickoff_utc','known_at_utc','source_observed_at_utc','score_semantics','regulation_points','status','certified']
    if any(k not in parent for k in required):raise ValueError('Certified regulation parent missing')
    if any(not isinstance(parent[k],str) or not parent[k].strip() for k in ['run_id','freeze_id','certificate_id']):raise ValueError('Immutable parent identity absent')
    if parent['game_id']!=game_id or parent['team']!=team:raise ValueError('Parent fixture/team mismatch')
    if parent['score_semantics']!='REGULATION_ONLY' or parent['certified'] is not True or parent['status']!='PRE_KICKOFF':
        raise ValueError('Parent is not certified pregame regulation expectation')
    for field in ['model_sha256','feature_sha256']:
        if not re.fullmatch('[0-9a-f]{64}',str(parent[field])):raise ValueError('Parent hash missing')
    n=pd.Timestamp(now);kickoff=pd.Timestamp(parent['kickoff_utc']);known=pd.Timestamp(parent['known_at_utc']);observed=pd.Timestamp(parent['source_observed_at_utc'])
    if any(t.tzinfo is None for t in [n,kickoff,known,observed]) or known>observed or observed>n or n>=kickoff-pd.Timedelta(minutes=1):raise ValueError('Parent clock/cutoff invalid')
    value=float(parent['regulation_points'])
    if not np.isfinite(value) or value<0:raise ValueError('Parent regulation points invalid')
    if 'expected_final_points' in parent:
        ot=parent.get('expected_ot_points')
        if ot is None or not np.isfinite(ot) or ot<0 or not np.isclose(value+ot,parent['expected_final_points'],atol=1e-10):raise ValueError('Explicit OT/final contract mismatch')
    return value

def quarter_contract(parent,shares,*,game_id,team,now,allocation_certificate=None):
    try:
        reg=require_regulation_parent(parent or {},game_id=game_id,team=team,now=now)
        c=allocation_certificate or {}
        if (c.get('status')!='CERTIFIED_PRODUCTION' or not c.get('certificate_id') or c.get('research_only') is not False
            or c.get('score_semantics')!='REGULATION_ONLY' or c.get('parent_model_sha256')!=parent['model_sha256']
            or c.get('calibration_validated') is not True or not re.fullmatch('[0-9a-f]{64}',str(c.get('model_sha256','')))):
            raise ValueError('Allocation model unpromoted or incompatible with parent')
        from nfl2026.phase7.lab import promotion_decision
        if promotion_decision(c.get('gates',{}))['blocked_gates']:raise ValueError('Allocation certification gates failed')
        trained=pd.Timestamp(c.get('training_max'));validated=pd.Timestamp(c.get('validated_at_utc'));expiry=pd.Timestamp(c.get('valid_until_utc'))
        if any(pd.isna(t) or t.tzinfo is None for t in [trained,validated,expiry]) or not trained<validated<=pd.Timestamp(now)<expiry:
            raise ValueError('Allocation certificate clocks/expiry invalid')
        source=c.get('fixture_allocations',{}).get(game_id+'|'+team,{})
        fitted_shares=np.asarray(source.get('shares',[]),float);requested=np.asarray(shares,float)
        if (source.get('parent_run_id')!=parent['run_id'] or source.get('parent_feature_sha256')!=parent['feature_sha256']
            or source.get('model_sha256')!=c['model_sha256'] or not re.fullmatch('[0-9a-f]{64}',str(source.get('feature_sha256','')))
            or fitted_shares.shape!=(4,) or requested.shape!=(4,) or not np.isfinite(fitted_shares).all()
            or not np.isfinite(requested).all() or (fitted_shares<0).any() or (requested<0).any()
            or fitted_shares.sum()<=0 or requested.sum()<=0
            or not np.allclose(fitted_shares/fitted_shares.sum(),requested/requested.sum(),rtol=0,atol=1e-12)):
            raise ValueError('Quarter allocation differs from independently pinned inference source')
        source_time=pd.Timestamp(source.get('known_at_utc'))
        if pd.isna(source_time) or source_time.tzinfo is None or source_time>pd.Timestamp(now):raise ValueError('Allocation source clock invalid')
    except (ValueError,TypeError,KeyError) as e:
        return dict(schema='quarter-score-v1',game_id=game_id,team=team,status='WITHHELD',reason=str(e),
            q1=None,q2=None,q3=None,q4=None,first_half=None,second_half=None,regulation_points=None,
            expected_ot_points=None,expected_final_points=None,point_semantics='EXPECTED_POINTS',created_at_utc=str(now),actionable=False)
    values=allocate_regulation(reg,shares)
    return dict(schema='quarter-score-v1',game_id=game_id,team=team,status='CERTIFIED_PRODUCTION',
        **dict(zip(Q,map(float,values))),first_half=float(values[:2].sum()),second_half=float(values[2:].sum()),
        regulation_points=reg,expected_ot_points=parent.get('expected_ot_points'),expected_final_points=parent.get('expected_final_points'),
        parent_run_id=parent['run_id'],parent_freeze_id=parent['freeze_id'],parent_model_sha256=parent['model_sha256'],
        parent_feature_sha256=parent['feature_sha256'],parent_certificate_id=parent['certificate_id'],
        parent_score_semantics='REGULATION_ONLY',allocation_model_sha256=allocation_certificate['model_sha256'],
        allocation_feature_sha256=source['feature_sha256'],allocation_shares=(fitted_shares/fitted_shares.sum()).tolist(),
        allocation_certificate_id=allocation_certificate.get('certificate_id'),point_semantics='EXPECTED_POINTS',created_at_utc=str(now),actionable=False)
