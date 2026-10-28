"""Matched fixed-policy receiver recall and a publication-ineligible UI candidate."""
from datetime import datetime,timezone
import json
import shutil

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from nfl2026.frontend.receiver_discovery import categorize,build_current_contract
from nfl2026.certification.players import asof_depth
from nfl2026.research.rigorous_review import cluster_ci
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file


def validation(root,out):
    d=pd.read_csv(root/'reports/rigorous_benchmark_week7_20261022/wr_growth_signal_predictions.csv')
    records=[];parts=[]
    for season in [2023,2024,2025]:
        b=d[d.season.eq(season)].copy()
        populations=[('NOMINAL_RESEARCH',b)]
        if season==2025:
            b['cutoff_utc']=(pd.to_datetime(b.kickoff_utc,utc=True)-pd.Timedelta(hours=24)).astype(str)
            depth=pd.read_csv(root/'NFL Fantasy/2025 Stats/depth_charts_2025.csv',low_memory=False)
            starter=asof_depth(depth,b)
            populations.append(('TIMESTAMPED_DEPTH_STARTERS_AVAILABILITY_UNPROVED',starter[starter.expected_starter_depth]))
        for population,b in populations:
            ranked=categorize(b);high=ranked.actual_targets.ge(5);emerging=ranked.prior8_targets.lt(5)&high
            for name,mask in [('TOP_TWO',ranked.target_rank.le(2)),('FEATURED_PLUS_RADAR',ranked.category.isin(['FEATURED','UNDER_THE_RADAR']))]:
                chosen=ranked[mask];recall=chosen.actual_targets.ge(5).sum()/high.sum()
                gain=(ranked.category.isin(['FEATURED','UNDER_THE_RADAR']).astype(float)-ranked.target_rank.le(2).astype(float))[high]
                ci=cluster_ci(ranked[high],gain)
                records.append({'season':season,'population':population,'selector':name,'eligible':len(ranked),'selected':len(chosen),
                    'recall':recall,'precision':chosen.actual_targets.ge(5).mean(),'false_positive_under2_fraction':chosen.actual_targets.lt(2).mean(),
                    'emerging_recall':int((mask&emerging).sum())/emerging.sum(),'emerging_outcomes':int(emerging.sum()),
                    'recall_gain_ci_low':ci[0],'recall_gain_ci_high':ci[1],
                    'targets_average_precision':average_precision_score(high,ranked.expected_targets),
                    'yardage_average_precision':average_precision_score(high,ranked.pred_receiving_yards),
                    'automatic_role_promotion':False})
            parts.append(ranked.assign(population=population))
    pd.DataFrame(records).to_csv(out/'wr_forward_validation.csv',index=False)
    pd.concat(parts,ignore_index=True).to_csv(out/'wr_ranked_validation_rows.csv',index=False)


def build_candidate(settings,out):
    """Original numerical assets remain byte-identical. No global selector changes."""
    from nfl2026.frontend.v3_release import UI_FILES,digest_files,validate_release,audit_data
    root=settings.root;out=out.resolve()
    previous=root/'frontend/releases/frontend-v3-1-57e41c99d7a69eb1'
    candidate=out/'ui_candidate'
    if candidate.exists():raise FileExistsError('Receiver UI candidate is immutable')
    shutil.copytree(previous,candidate)
    contract=build_current_contract(settings)
    json_write(candidate/'data/receiver_discovery.json',contract)
    shutil.copyfile(root/'frontend/phase7_src/receiver_discovery_v2.js',candidate/'receiver_discovery_v2.js')
    html=(candidate/'index.html').read_text(encoding='utf-8')
    # JSON script is data, not executable code; escape HTML parser delimiters.
    encoded=json.dumps(contract,separators=(',',':')).replace('<','\\u003c')
    token='<script src="phase7.js'
    if token not in html:raise ValueError('Original frontend script ordering changed')
    html=html.replace(token,'<script id="receiver-discovery-evidence" type="application/json">'+encoded+'</script><script src="receiver_discovery_v2.js?v='+sha256_file(candidate/'receiver_discovery_v2.js')[:16]+'"></script>'+token,1)
    (candidate/'index.html').write_text(html,encoding='utf-8')
    current=json.loads((candidate/'data/current_week.json').read_text())
    ui=(*UI_FILES,'v31.js','receiver_discovery_v2.js')
    files={str(p.relative_to(candidate)).replace('\\','/'):sha256_file(p) for p in candidate.rglob('*') if p.is_file() and p.name not in ['release_manifest.json','frontend_release.json']}
    content=digest_files(files);rid='frontend-v3-1-'+content[:16]
    identity={'release_id':rid,'prediction_run_id':current['metadata']['run_id'],
        'ui_sha256':{name:files[name] for name in ui},'data_sha256':{name:digest for name,digest in files.items() if name.startswith('data/')}}
    json_write(candidate/'data/frontend_release.json',identity)
    parity=audit_data(settings,candidate)
    manifest={'status':'VALIDATED_RELEASE_CANDIDATE_NOT_PUBLISHED','release_id':rid,'content_sha256':content,
        'prediction_run_id':current['metadata']['run_id'],'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'files':{str(p.relative_to(candidate)).replace('\\','/'):sha256_file(p) for p in candidate.rglob('*') if p.is_file() and p.name!='release_manifest.json'},
        'data_parity':parity,'research_forecasts_included':False,'official_pointer_updated':False,'frontend_version':'3.1',
        'receiver_discovery_schema':'receiver-discovery-v2'}
    json_write(candidate/'release_manifest.json',manifest)
    validate_release(candidate,settings=settings)
    json_write(out/'ui_candidate.json',{'path':str(candidate.relative_to(root)),'release_id':rid,'manifest_sha256':sha256_file(candidate/'release_manifest.json'),
        'official_run_changed':False,'publication_review':'PENDING','published':False})
    return candidate
