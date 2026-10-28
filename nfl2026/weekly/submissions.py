"""Opt-in scoring of future prospective challengers already frozen before kickoff."""
from pathlib import Path
import pandas as pd
from nfl2026.research.frozen_evaluation import verify_freeze,require_pregame_rows
from nfl2026.weekly.metrics import player_metrics,quarter_metrics
from nfl2026.weekly.catalog import point_comparison
from nfl2026.utilities.io import sha256_file

def evaluate_submissions(root,entries,games,official,labels,quarters):
    root=Path(root);results=[];sources={};seen={}
    for entry in entries:
        folder=(root/entry['freeze_path']).resolve()
        if not folder.is_relative_to(root):raise ValueError('External challenger paths are unsupported')
        manifest=verify_freeze(folder,games.kickoff_utc.min());path=folder/entry['predictions_file']
        listed=manifest.get('files',manifest.get('file_sha256',{}))
        if entry['predictions_file'] not in listed:raise ValueError('Challenger prediction not hash-bound by freeze')
        origin=manifest['run_id'];identity=sha256_file(path)
        if origin in seen:
            if seen[origin]!=identity:raise ValueError('Conflicting registered forecast origin')
            results.append(dict(origin_id=origin,status='NOT_COUNTED_AGAIN',kind='ORIGIN_ALIAS'));continue
        seen[origin]=identity;d=pd.read_csv(path);require_pregame_rows(d,entry.get('creation_column','created_at_utc'))
        if not set(d.game_id).issubset(set(games.game_id)):raise ValueError('Challenger includes another fixture population')
        clock=games.set_index('game_id').kickoff_utc
        if any(pd.Timestamp(r.kickoff_utc)!=pd.Timestamp(clock.loc[r.game_id]) for r in d.itertuples()):raise ValueError('Challenger kickoff identity differs')
        kind=entry['kind'];result=dict(origin_id=origin,kind='PROSPECTIVE_FROZEN_RESEARCH',promotion_decision='HOLD_SEPARATE_VALIDATION_REQUIRED')
        if kind=='PLAYER':
            m,_,_=player_metrics(d,labels);result['metrics']=m.where(m.notna(),None).to_dict('records')
        elif kind=='QUARTERS':result['metrics']=quarter_metrics(d,quarters)
        elif kind=='TEAMS':result['metrics']=point_comparison(d,official,games,'home_expected_points','away_expected_points')
        else:raise ValueError('Unsupported challenger schema; no prediction fallback')
        results.append(result)
        for p in [folder/'freeze_manifest.json',path]:sources[str(p.relative_to(root))]=sha256_file(p)
    return results,sources
