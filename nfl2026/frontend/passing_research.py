"""Local-only passing preview contract, separate from certified-passing-v1."""
from __future__ import annotations
import math

def validate_research_contract(contract):
    if contract.get('schema')!='passing-research-preview-v1' or contract.get('status')!='EXPERIMENTAL_NOT_PROMOTED':
        raise ValueError('Research passing contract required')
    seen=set()
    for row in contract.get('week8',[]):
        key=(row['game_id'],row['team'])
        if key in seen:raise ValueError('Duplicate fixture QB slot')
        seen.add(key)
        if row.get('status')!='WITHHELD' or row.get('actionable') is not False:
            raise ValueError('Uncertified preview cannot become production')
        for field in ['projected_yards','projected_attempts','interval','over_probability','fair_odds','bookmaker_comparison']:
            if row.get(field) is not None:raise ValueError('Uncertified projection/market leaked')
    for row in contract.get('historical_examples',[]):
        if row.get('purpose')!='RETROSPECTIVE_BENCHMARK' or not row.get('player_id'):
            raise ValueError('Unidentified historical example')
        for field in ['projected_yards','projected_attempts']:
            if not math.isfinite(row.get(field,float('nan'))) or row[field]<0:raise ValueError('Invalid historical example')
    return True
