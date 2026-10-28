"""A scoped, immutable review request. It grants no approval or publication rights."""
from pathlib import Path
from datetime import datetime,timezone
from nfl2026.frontend.v32_release import read,write,validate_candidate
from nfl2026.utilities.io import sha256_file

RECEIPTS=['candidate_validation.json','browser_qa_receipt.json','theme_accessibility_receipt.json','data_contract_receipt.json',
          'reviewed_overlay_actual_time_receipt.json','final_python_test_receipt.json','local_preview_receipt.json',
          'archived_receiver_contract_audit.json','protected_after_receipt.json']

def create_global_review_candidate(report,root):
    report=Path(report).resolve();root=Path(root).resolve()
    if not report.is_relative_to(root/'reports'):raise ValueError('Scoped local report required')
    target=report/'global_approval_candidate.json'
    if target.exists():raise ValueError('Review candidate is immutable; use a new report')
    result=validate_candidate(report/'candidate',root);manifest=report/'candidate/release_manifest.json';manifest_sha=sha256_file(manifest)
    pins={}
    for n in RECEIPTS:
        p=report/n;r=read(p)
        if r.get('status')!='PASS':raise ValueError('Review evidence is incomplete: '+n)
        if 'release_id' in r and r['release_id']!=result['release_id']:raise ValueError('Review release differs: '+n)
        for key in ['manifest_sha256','release_manifest_sha256']:
            if key in r and r[key]!=manifest_sha:raise ValueError('Review fingerprint differs: '+n)
        pins[n]=sha256_file(p)
    request=dict(schema='frontend-v32-global-review-candidate-v1',created_at_utc=datetime.now(timezone.utc).isoformat(),
        status='READY_FOR_SEPARATE_GLOBAL_REVIEW',release_id=result['release_id'],release_path=str((report/'candidate').relative_to(root)),
        release_manifest_sha256=manifest_sha,base_release_id='frontend-v3-1-0aa9d638d7215e81',evidence_sha256=pins,
        requested_scope='V3.2 historical presentation and explicitly withheld uncertified extensions',
        original_forecast_replacement=False,model_promotion=False,publication_authorised=False,human_approval='PENDING',
        global_selector_updated=False,current_public_release_unchanged=True,
        data_gate='PASS_FROZEN_IDENTITIES_AND_SEPARATE_VALIDATED_FINAL_RESULTS',ui_gate='PASS_LOCAL_CONTRACTS_AND_BROWSER_QA',
        official_week8_forecast_gate='BLOCKED_UPSTREAM_NO_NEW_FORECASTS',passing_gate='WITHHELD_NOT_CERTIFIED',
        quarter_gate='WITHHELD_NOT_CERTIFIED',value_gate='WITHHELD_NO_CURRENT_CERTIFIED_COMPARISON',
        deployment_gate='PENDING_SEPARATE_GLOBAL_REVIEW_AND_EXPLICIT_DEPLOYMENT_APPROVAL',
        limitations=['Original availability overlay is expired and all Week7 fixtures are closed; no player is actionable.',
          'The original78WR discovery signal uses history through2025; no current-form claim is made.',
          'Legacy mutable performance generator lacks a completed-week evaluation report; the candidate uses independently reproduced immutable official team metrics.',
          'This scoped review request is not an existing global approval, a deployment token or a model certification. The existing publisher must not accept it as one.'])
    write(target,request);return request

def validate_global_review_candidate(report,root):
    report=Path(report);r=read(report/'global_approval_candidate.json');result=validate_candidate(report/'candidate',root)
    if r['release_id']!=result['release_id'] or r['release_manifest_sha256']!=sha256_file(report/'candidate/release_manifest.json'):raise ValueError('Reviewed candidate changed')
    if r['publication_authorised'] is not False or r['human_approval']!='PENDING' or r['global_selector_updated'] is not False:raise ValueError('Scoped request cannot self-approve')
    for n,h in r['evidence_sha256'].items():
        if sha256_file(report/n)!=h:raise ValueError('Review evidence changed: '+n)
    return dict(status='PASS',release_id=r['release_id'],global_review='PENDING',publication_authorised=False)
