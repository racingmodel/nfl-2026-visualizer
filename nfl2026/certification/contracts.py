"""Explicit certificate/label boundaries; incomplete evidence never activates a model."""
from pathlib import Path
import json

from nfl2026.phase7.lab import promotion_decision
from nfl2026.utilities.io import sha256_file


def passing_package(artifact,metadata,gates,*,label_resolution):
    import joblib
    record=json.loads(metadata.read_text())
    if sha256_file(artifact)!=record['sha256']:raise ValueError('Passing artifact hash mismatch')
    bundle=joblib.load(artifact)
    if not hasattr(bundle.get('models',{}).get('passing_yards'),'predict') or not bundle.get('features'):raise ValueError('Passing feature/model package missing')
    if any(x.startswith(('actual_','target_')) or any(t in x.lower() for t in ['odds','bookmaker']) for x in bundle['features']):
        raise ValueError('Passing feature leakage')
    gates=dict(gates)
    if not isinstance(label_resolution,dict) or label_resolution.get('status')!='PASS':
        gates['pit_provenance']=False
    decision=promotion_decision(gates)
    return {'schema':'passing-model-package-v1','status':'CERTIFIABLE_PENDING_APPROVAL' if not decision['blocked_gates'] else 'BLOCKED',
        'artifact_path':str(artifact),'artifact_sha256':record['sha256'],'metadata_sha256':sha256_file(metadata),
        'feature_schema':bundle['features'],'training_cutoff':record['provenance']['max_training_kickoff_utc'],
        'label_contract':'individual-passing-pbp-v1','label_resolution':label_resolution,
        'production_registered':False,'gate_evidence':gates,**decision}


def require_grading_equivalence(model_contract,quote,rule):
    """No jurisdiction/period/kneeldown assumption from a generic bookmaker name."""
    import re
    import pandas as pd
    if not quote.get('jurisdiction') or not quote.get('rule_version'):raise ValueError('Quote grading jurisdiction/version unavailable')
    required=['bookmaker','jurisdiction','rule_version','market_type','stat_contract']
    if any(not rule.get(key) for key in required):raise ValueError('Grading rule contract incomplete')
    if not re.fullmatch(r'[0-9a-f]{64}',str(rule.get('source_sha256',''))) or not rule.get('source_url'):
        raise ValueError('Authoritative rule evidence unavailable')
    known=pd.to_datetime(rule.get('known_at_utc'),utc=True,errors='coerce')
    quote_time=pd.to_datetime(quote.get('source_timestamp_utc'),utc=True,errors='coerce')
    if pd.isna(known) or pd.isna(quote_time) or known>quote_time:raise ValueError('Rule was not evidenced at quote time')
    if any(quote.get(key)!=rule[key] for key in ['bookmaker','jurisdiction','rule_version','market_type']):raise ValueError('Grading scope differs')
    if model_contract!=rule['stat_contract']:raise ValueError('Model/bookmaker label semantics differ')
    return True
