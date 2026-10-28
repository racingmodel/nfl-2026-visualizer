"""Presentation-only opportunity evidence bound to the original forecast inputs."""
from datetime import datetime,timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.utilities.io import sha256_file

SCHEMA='receiver-discovery-v2'
RULE='top2-targets-plus-prior3-growth-top3-v1'


def categorize(frame):
    """No player/role/yardage changes; unknown usage stays in completeness view."""
    d=frame.sort_values(['game_id','team','expected_targets','player_id'],ascending=[True,True,False,True]).copy()
    if d.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate receiver identity')
    d['target_rank']=d.groupby(['game_id','team']).expected_targets.rank(method='first',ascending=False)
    d['recent_rank']=d.groupby(['game_id','team']).prior3_targets.rank(method='min',ascending=False)
    complete=d[['expected_targets','prior3_targets','prior8_targets']].notna().all(axis=1)
    established=complete&d.target_rank.le(2)
    growth=complete&~established&d.prior3_targets.gt(d.prior8_targets)&d.recent_rank.le(3)
    d['category']=np.select([established,growth],['FEATURED','UNDER_THE_RADAR'],default='ALL_ELIGIBLE')
    return d


def build_current_contract(settings):
    root=settings.root
    bundle=json.loads((root/'frontend/dist/data/current_week.json').read_text())
    meta=bundle['metadata'];season=int(meta['season']);week=int(meta['week']);rid=meta['run_id']
    run=root/f'predictions/{season}/week_{week:02d}'/rid
    modelmeta=json.loads((run/'metadata.json').read_text());cutoff=pd.Timestamp(modelmeta['cutoff_utc'])
    from nfl2026.production.core import verify_lock
    lock=root/f"locks/{season}/week_{week:02d}/{modelmeta['lock_id']}"
    if verify_lock(lock):raise ValueError('Original lock failed integrity')
    for name,digest in modelmeta['file_hashes'].items():
        if sha256_file(run/name)!=digest:raise ValueError('Original forecast changed')
    roll_path=root/f'artifacts/phase2/rollforward_manifests/{season}/week_{week:02d}.json'
    roll=json.loads(roll_path.read_text())
    weekly=root/'artifacts/canonical/player_weekly.csv';schedule=root/'artifacts/canonical/canonical_games.csv'
    if roll['source_completed_week']!=week-1 or sha256_file(weekly)!=roll['player_actuals_sha256']:
        raise ValueError('Usage inputs differ from original forecast feature inputs')
    actual_manifest=root/roll['source_actual_manifest_path']
    if sha256_file(actual_manifest)!=roll['source_actual_manifest_sha256']:raise ValueError('Actual source evidence changed')
    known=pd.Timestamp(json.loads(actual_manifest.read_text())['retrieved_at_utc'])
    if known>cutoff or pd.Timestamp(roll['generated_at_utc'])>cutoff:raise ValueError('Usage inputs were unavailable at original cutoff')
    actual=pd.read_csv(weekly,low_memory=False).rename(columns={'targets':'target_targets'})
    games=pd.read_csv(schedule,usecols=['game_id','season','week','game_type','home_team','away_team','kickoff_utc'],low_memory=False)
    games=games[games.game_type.eq('REG')]
    mapping=pd.concat([games.rename(columns={'home_team':'team'})[['game_id','season','week','team','kickoff_utc']],
        games.rename(columns={'away_team':'team'})[['game_id','season','week','team','kickoff_utc']]])
    actual=actual[actual.player_id.notna()].rename(columns={'opponent_team':'opponent'})
    actual=actual.merge(mapping,on=['season','week','team'],validate='many_to_one')
    actual=actual[pd.to_datetime(actual.kickoff_utc,utc=True).lt(cutoff)&((actual.season<season)|((actual.season==season)&(actual.week<week)))]
    # Restrict to players actually present in prior measured game observations.
    if actual.duplicated(['player_id','game_id']).any():raise ValueError('Ambiguous prior receiver observations')
    players=pd.read_csv(run/'players.csv');players=players[players.position.eq('WR')].copy()
    game_times=pd.read_csv(run/'games.csv',usecols=['game_id','kickoff_utc'])
    players=players.merge(game_times,on='game_id',validate='many_to_one')
    from nfl2026.research.review_inputs import prior_target_means
    values=prior_target_means(players,actual[['game_id','player_id','kickoff_utc','target_targets']])
    # Use same primary eligibility predicate as the current published source proof.
    from nfl2026.phase7.player_integrity import audit_frozen_players
    _,proof,_=audit_frozen_players(settings,rid,week,season)
    good=[]
    displayed={f"{g['game_id']}|{p['player_id']}|{p['team']}" for g in bundle['games'] for p in g.get('players',[]) if p.get('position')=='WR'}
    for p in values.itertuples():
        key=f'{p.game_id}|{p.player_id}|{p.team}';e=proof.get(key,{})
        good.append(key in displayed and e.get('eligible') is True and e.get('expected_starter') is True and e.get('prediction_source_run_id')==rid)
    values=values[good]
    values=categorize(values)
    rows={}
    for p in values.itertuples():
        key=f'{p.game_id}|{p.player_id}|{p.team}'
        # Missing usage is an explicit completeness-only case, never a fake zero.
        rows[key]={'game_id':p.game_id,'player_id':p.player_id,'team':p.team,'prediction_source_run_id':rid,
            'cutoff_utc':cutoff.isoformat(),'usage_known_at_utc':known.isoformat(),
            'prior3_targets':float(p.prior3_targets) if pd.notna(p.prior3_targets) else None,
            'prior8_targets':float(p.prior8_targets) if pd.notna(p.prior8_targets) else None,
            'target_rank':int(p.target_rank),'recent_rank':float(p.recent_rank) if pd.notna(p.recent_rank) else None,'category':p.category}
    sources=[run/'metadata.json',run/'players.csv',run/'games.csv',lock/'data_lock.json',lock/'lock_manifest.csv',roll_path,weekly,actual_manifest]
    return {'schema':SCHEMA,'rule':RULE,'prediction_run_id':rid,'season':season,'week':week,
        'status':'PRESENTATION_ONLY','players':rows,'source_sha256':{str(p.relative_to(root)):sha256_file(p) for p in sources},
        'disclaimer':'Rising recent usage; original model projections. No new model or betting-value recommendation.',
        'historical_policy':'Missing same-run pregame usage evidence shows completeness view only'}


def validate_contract(settings,payload):
    if payload!=build_current_contract(settings):raise ValueError('Receiver discovery differs from frozen input/source contract')
    return True


def validate_release_contract(settings,candidate,manifest):
    path=candidate/'data/receiver_discovery.json'
    declared=manifest.get('receiver_discovery_schema')
    if not path.exists() and declared is None:return
    if declared!=SCHEMA or not path.exists():raise ValueError('Receiver discovery release contract missing')
    module=candidate/'receiver_discovery_v2.js'
    if not module.is_file():raise ValueError('Receiver discovery module missing')
    payload=json.loads(path.read_text())
    if settings is not None:validate_contract(settings,payload)
    elif payload.get('schema')!=SCHEMA or payload.get('rule')!=RULE:raise ValueError('Unknown receiver discovery policy')
    import re
    html=(candidate/'index.html').read_text(encoding='utf-8')
    match=re.search(r'<script id="receiver-discovery-evidence" type="application/json">(.*?)</script>',html,re.S)
    if not match or json.loads(match.group(1))!=payload:raise ValueError('Inline receiver evidence differs from hashed payload')
    if f'receiver_discovery_v2.js?v={sha256_file(module)[:16]}' not in html:raise ValueError('Receiver discovery cache identity missing')
