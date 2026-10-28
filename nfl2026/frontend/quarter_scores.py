"""Quarter data audit against separately verified immutable parent/cert sources.

This module neither loads a public selector nor writes/publishes a release.
The normal model and frontend release approvals remain separate requirements.
"""
from __future__ import annotations
import numpy as np
from nfl2026.research.quarter_phase4 import Q,quarter_contract

def audit_quarter_bundle(bundle,parents=None,certificates=None):
    if bundle.get('schema')!='quarter-score-bundle-v1':raise ValueError('Quarter bundle schema')
    parents=parents or {};certificates=certificates or {};fixtures={g['game_id']:g for g in bundle.get('fixtures',[])}
    if len(fixtures)!=len(bundle.get('fixtures',[])):raise ValueError('Duplicate quarter fixture')
    seen=set();certified=0
    for row in bundle.get('rows',[]):
        key=(row.get('game_id'),row.get('team'))
        if key in seen:raise ValueError('Duplicate quarter row')
        seen.add(key);g=fixtures.get(key[0])
        if not g or key[1] not in [g['away_team'],g['home_team']]:raise ValueError('Quarter fixture/team mismatch')
        if row.get('point_semantics')!='EXPECTED_POINTS':raise ValueError('Ambiguous quarter point semantics')
        if row.get('status')=='WITHHELD':
            if any(row.get(c) is not None for c in Q+['first_half','second_half','regulation_points','expected_ot_points','expected_final_points']):
                raise ValueError('Unsupported quarter number leaked')
            continue
        if row.get('status')!='CERTIFIED_PRODUCTION':raise ValueError('Uncertified quarter row')
        parent=parents.get(key);certificate=certificates.get(row.get('allocation_certificate_id'))
        if not parent or not certificate:raise ValueError('Independent parent/certificate source absent')
        if parent['kickoff_utc']!=g['kickoff_utc']:raise ValueError('Quarter parent kickoff differs')
        shares=row.get('allocation_shares',[])
        rebuilt=quarter_contract(parent,shares,game_id=key[0],team=key[1],now=row['created_at_utc'],allocation_certificate=certificate)
        if rebuilt['status']!='CERTIFIED_PRODUCTION':raise ValueError('Quarter source gates failed')
        for name in Q+['first_half','second_half','regulation_points','expected_ot_points','expected_final_points']:
            a,b=row.get(name),rebuilt.get(name)
            if (a is None)!=(b is None) or (a is not None and not np.isclose(a,b,rtol=0,atol=1e-10)):
                raise ValueError('Quarter points differ from immutable source')
        for name in ['parent_run_id','parent_freeze_id','parent_model_sha256','parent_feature_sha256','parent_certificate_id','allocation_model_sha256','allocation_feature_sha256']:
            if row.get(name)!=rebuilt.get(name):raise ValueError('Quarter lineage differs')
        certified+=1
    required={(gid,t) for gid,g in fixtures.items() for t in [g['away_team'],g['home_team']]}
    if seen!=required:raise ValueError('Incomplete quarter population')
    return dict(fixtures=len(fixtures),rows=len(seen),certified_rows=certified,withheld_rows=len(seen)-certified,
        publication_authorised=False,independent_parent_sources_required_for_certified_rows=True,verified_parent_source_rows=certified)
