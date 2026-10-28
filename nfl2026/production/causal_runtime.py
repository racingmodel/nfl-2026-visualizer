"""Proposed independent causal contract; no legacy selector/gate changes.

Research EARLY_WEEK is explicitly authorised separately. Official activation
requires an approved protocol AND independently passed model/source certificates.
"""
from pathlib import Path
import hashlib,json
import numpy as np,pandas as pd
from nfl2026.research.causal_baseline import FEATURES,VERSION,schema_hash,predict_research
from nfl2026.research.prospective_shadow import assess_fixture as strict_research,utc,true,TEAM_SHA

CONTRACT=dict(schema='causal-team-runtime-v1',feature_version=VERSION,features=FEATURES,feature_schema_sha256=schema_hash(),
    historical_expected_QB_identity_features=[],training_priors='Fitted on training rows only; explicit missing indicators; reject all-missing training columns',
    history_window=8,history_availability='48hour conservative event embargo plus current captured source proof',target='FINAL_TEAM_POINTS_INCLUDING_OVERTIME',
    independent_components={'team':'7score-historyfeatures','player':'23rawusagefeatures; no score input','td':'Separate parent-budget certificate required'},
    levels=['EARLY_WEEK','GAME_DAY_CERTIFIED'],official_activation='APPROVED_PROTOCOL_AND_MODEL_CERTIFICATE_AND_SOURCE_CERTIFICATE_REQUIRED',
    proposal_only=True,original_production_gate_changes=False)

def contract_hash():return hashlib.sha256(json.dumps(CONTRACT,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def assess_fixture(game,states,qbs,coverage,roster,now,schedule_observed,*,level='EARLY_WEEK'):
    """New stage proposal; original source rows are preserved in the result."""
    if level not in CONTRACT['levels']:raise ValueError('Unknown readiness level')
    now=utc(now);local=coverage.copy();pending=[];raw=[]
    for i,r in local[local.game_id.eq(game['game_id'])].iterrows():
        raw.append(dict(team=r.team,source_state=r.source_state,records=r.get('records'),report_empty=r.get('report_empty'),coverage_status=r.get('coverage_status')))
        # The league's undated empty marker is not a healthy/complete report.
        # Before the normal first deadline EARLY_WEEK retains UNKNOWN/PENDING.
        if level=='EARLY_WEEK' and r.source_state=='VALID' and pd.to_numeric(r.get('records'),errors='coerce')==0 and true(r.get('report_empty',False)) and now<utc(r.first_report_due_utc) and not true(r.complete_final_club_report) and not true(r.complete_practice_club_report):
            local.loc[i,'source_state']='NOT_YET_PUBLISHED';pending.append(r.team)
    result=strict_research(game,states,qbs,local,roster,now,schedule_observed)
    result.update(readiness_contract=CONTRACT['schema'],readiness_level=level,injury_report_pending_teams=pending,raw_source_coverage=raw,
        pending_interpretation='Derived early-week UNKNOWN/PENDING readiness, not a new claim of publication, an empty injury list or clearance',
        proposal_approved_for_official=False,stage_source_eligible=result['eligible'],official_eligible=False)
    if level=='GAME_DAY_CERTIFIED':
        pair=local[local.game_id.eq(game['game_id'])]
        if len(pair)!=2 or not all(true(v) for v in pair.complete_final_club_report):result['reasons'].append('PAIRED_FINAL_REPORT_CERTIFICATE_REQUIRED')
        if utc(game['kickoff_utc'])-now<=pd.Timedelta(minutes=90):result['reasons'].append('STRICT_BOTH_OWN_INACTIVES_CERTIFICATE_REQUIRED')
        # This API does not manufacture a source certificate from booleans.
        result['reasons'].append('EXISTING_FULL_SOURCE_AND_MODEL_CERTIFICATION_BOUNDARY_REQUIRED')
        result['eligible']=False;result['stage_source_eligible']=False
    return result

def predict_points(package,frame):
    """Exact saved seven-input means; experimental probability stays internal."""
    if package.get('feature_version')!=VERSION or list(frame.columns)!=FEATURES:raise ValueError('Causal runtime schema differs')
    return predict_research(package,frame)

def certification_assessment(phase12):
    phase12=Path(phase12);audit=json.loads((phase12/'final_certification_assessment.json').read_text(encoding='utf-8'));folds=json.loads((phase12/'baseline_audit/fold_metrics.json').read_text(encoding='utf-8'))
    # Proposal uses an existing absolute accuracy hurdle and the existing value
    # calibration floor. Neither replaces stronger original promotion policy.
    # This materially different certification protocol is NOT auto-approved.
    holdouts=[r for r in folds if r['season']>=2023]
    criteria=dict(independent_protocol_approval=False,historical_publication_and_revision_provenance=False,
        chronological_computation_and_saved_parity=True,training_only_priors=True,historical_QB_metadata_dependency_absent=True,
        existing_absolute_score_MAE_hurdle_7_552=all(r['team_score_mae']<=7.552 for r in holdouts),
        independent_calibration_min200_ECE005=all(r['non_tied_games']>=200 and r['win_ece']<=.05 for r in holdouts),
        bias_and_secondary_quality=audit['bias_noninferiority'] and audit['secondary_margin_total_noninferiority'],
        exact_model_feature_runtime=True,certified_score_distribution_and_tie_contract=False,full_player_TD_parent_output_compatibility=False)
    return dict(schema='independent-causal-new-model-proposal-v1',contract_sha256=contract_hash(),status='BLOCKED_NOT_ACTIVATABLE',criteria=criteria,
        failed=[k for k,v in criteria.items() if v is not True],sole_unverified_incumbent_superiority_required=False,
        criteria_origin={'scoreMAE':'Existing Phase1 7.552 hurdle, applied to every fixed evaluation season including current2026; not tuned to permit baseline','calibration':'Existing certified-value floor n>=200 and ECE<=.05; not a substitute for full model/distribution certification','bias_secondary':'Existing measured safety/noninferiority failures retained'},
        approval_required='Explicit approval of this materially different certification protocol BEFORE any official activation; approval cannot cure failed data/quality gates',
        activation=False,existing_promotion_rules_changed=False,independent_historical_holdout_limit='Previously examined folds are reproduced evidence, not new untouched holdouts')

def require_official_activation(assessment,approval=None):
    required={'independent_protocol_approval','historical_publication_and_revision_provenance','chronological_computation_and_saved_parity','training_only_priors','historical_QB_metadata_dependency_absent','existing_absolute_score_MAE_hurdle_7_552','independent_calibration_min200_ECE005','bias_and_secondary_quality','exact_model_feature_runtime','certified_score_distribution_and_tie_contract','full_player_TD_parent_output_compatibility'}
    if assessment.get('status')!='CERTIFIED' or assessment.get('contract_sha256')!=contract_hash() or set(assessment.get('criteria',{}))!=required or any(v is not True for v in assessment['criteria'].values()):raise ValueError('Independent causal model certificate blocked; no official inference/activation')
    if not approval or approval.get('decision')!='APPROVED' or approval.get('contract_sha256')!=contract_hash() or approval.get('model_sha256')!=TEAM_SHA:raise ValueError('Exact model/protocol approval required')
    if utc(approval['approved_at_utc'])>pd.Timestamp.now(tz='UTC') or utc(approval['expires_at_utc'])<=pd.Timestamp.now(tz='UTC'):raise ValueError('Approval future/expired')
    return True

def validate_training_panel(frame,cutoff,provenance):
    """No expected-QB IDs. Reject unknown source clocks before fitting."""
    cutoff=utc(cutoff);required={'game_id','kickoff_utc','feature_known_at_utc','label_known_at_utc','home_score','away_score',*FEATURES}
    if not required.issubset(frame) or frame.game_id.duplicated().any():raise ValueError('Causal training panel schema/identity incomplete')
    if provenance.get('status')!='VERIFIED_ORIGINAL_ASOF_SOURCES' or not provenance.get('source_manifest_path'):raise ValueError('Historical as-of provenance unproved; no training')
    manifest_path=Path(provenance['source_manifest_path'])
    if not manifest_path.is_file() or hashlib.sha256(manifest_path.read_bytes()).hexdigest()!=provenance.get('source_manifest_sha256'):raise ValueError('Training source manifest unbound')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'));records=manifest.get('rows',{})
    from nfl2026.phase7.provenance import certified_pregame
    from nfl2026.live.pipeline import normalize_schedule_kickoffs
    from nfl2026.research.causal_baseline import score_history_inputs
    for r in frame.itertuples():
        if not utc(r.feature_known_at_utc)<utc(r.kickoff_utc)-pd.Timedelta(minutes=1) or not utc(r.kickoff_utc)<utc(r.label_known_at_utc)<=cutoff:raise ValueError('Training chronology invalid')
        evidence=records.get(r.game_id,{})
        if not certified_pregame(evidence,utc(r.kickoff_utc)-pd.Timedelta(minutes=1)):raise ValueError('Independent pregame snapshot evidence missing')
        raw=Path(evidence.get('path',''));labels=Path(evidence.get('label_path',''))
        if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest()!=evidence['source_sha256'] or not labels.is_file() or hashlib.sha256(labels.read_bytes()).hexdigest()!=evidence.get('label_sha256'):raise ValueError('Raw feature/label source hash differs')
        if utc(evidence['retrieved_at_utc'])!=utc(r.feature_known_at_utc) or utc(evidence['label_observed_at_utc'])!=utc(r.label_known_at_utc):raise ValueError('Training observation clocks differ')
        history=normalize_schedule_kickoffs(pd.read_csv(raw,encoding='utf-8',low_memory=False));history=history[history.season.ge(2022)&history.game_type.eq('REG')]
        built,_=score_history_inputs(history,asof=r.feature_known_at_utc);match=built[built.game_id.eq(r.game_id)]
        if len(match)!=1 or not np.allclose(match[FEATURES].to_numpy(float)[0],[getattr(r,f) for f in FEATURES],rtol=0,atol=1e-12,equal_nan=True):raise ValueError('Training features fail independent raw-source reconstruction')
        label=pd.read_csv(labels,encoding='utf-8',low_memory=False);label=label[label.game_id.eq(r.game_id)]
        if len(label)!=1 or label.iloc[0].home_score!=r.home_score or label.iloc[0].away_score!=r.away_score:raise ValueError('Training final labels differ')
    values=frame[FEATURES].to_numpy(float)
    if np.isinf(values).any() or np.isnan(values).all(axis=0).any():raise ValueError('Unsupported missing training feature')
    return frame[FEATURES]

def train_chronological(frame,cutoff,provenance):
    from nfl2026.research.team_phase7 import estimator
    x=validate_training_panel(frame,cutoff,provenance)
    payload=dict(features=FEATURES,feature_version=VERSION,schema_sha256=schema_hash())
    for side in ['home','away']:payload[side]=estimator('POISSON_BASE').fit(x,frame[side+'_score'])
    return payload

def certified_score_components(expected_home,expected_away,distribution_path=None,certificate=None):
    """Use existing event-supported score support; never round means into a PDF."""
    withheld=dict(home_win_probability=None,away_win_probability=None,tie_probability=None,snapped_home_score=None,snapped_away_score=None,status='WITHHELD_CERTIFIED_SCORE_DISTRIBUTION_AND_CALIBRATION_REQUIRED')
    if distribution_path is None or certificate is None:return withheld
    path=Path(distribution_path)
    if certificate.get('status')!='CERTIFIED' or certificate.get('parent_model_sha256')!=TEAM_SHA or hashlib.sha256(path.read_bytes()).hexdigest()!=certificate.get('distribution_sha256'):raise ValueError('Score component parent/hash certificate differs')
    if certificate.get('independent_holdout') is not True or certificate.get('all_quality_gates_passed') is not True or certificate.get('n',0)<200 or not 0<=certificate.get('ece',1)<=.05:raise ValueError('Score/win calibration not certified')
    import joblib
    from nfl2026.models.score_distribution import joint_distribution,score_summary
    model=joblib.load(path);p=joint_distribution(model,float(expected_home),float(expected_away));summary=score_summary(p)
    if abs(summary['discrete_home_mean']-expected_home)>1e-7 or abs(summary['discrete_away_mean']-expected_away)>1e-7:raise ValueError('Score distribution changed mean parent')
    home=float(np.tril(p,-1).sum());away=float(np.triu(p,1).sum());tie=float(np.trace(p))
    if abs(home+away+tie-1)>1e-8:raise ValueError('Winner/tie probability mass incoherent')
    return dict(home_win_probability=home,away_win_probability=away,tie_probability=tie,snapped_home_score=summary['headline_home_score'],snapped_away_score=summary['headline_away_score'],status='CERTIFIED_COMPONENT_ONLY;FULL_FIXTURE_AND_MODEL_GATES_STILL_REQUIRED')

def reconcile_certified_td(players,raw_probabilities,team_points,certificate=None):
    if certificate is None:return None,'WITHHELD_PARENT_POPULATION_CALIBRATION_CERTIFICATE'
    if certificate.get('status')!='CERTIFIED' or certificate.get('parent_model_sha256')!=TEAM_SHA or certificate.get('full_population_and_independent_calibration_passed') is not True:raise ValueError('TD compatibility certificate absent')
    from nfl2026.models.player_advanced import reconcile_td_probabilities
    if players.duplicated(['game_id','player_id','team']).any():raise ValueError('TD population duplicates')
    raw=np.asarray(raw_probabilities,float)
    if len(raw)!=len(players) or not np.isfinite(raw).all() or ((raw<0)|(raw>1)).any():raise ValueError('Raw TD probabilities invalid')
    output=np.zeros(len(players))
    for (gid,team),idx in players.reset_index(drop=True).groupby(['game_id','team']).groups.items():
        points=float(team_points[gid,team]);budget=float(np.clip(points/7*.72,.35,4.25));loc=np.asarray(list(idx))
        if 6*budget>points+1e-8:raise ValueError('Existing TD-budget floor incompatible with parent points')
        output[loc]=reconcile_td_probabilities(raw[loc],budget)
        if abs(output[loc].sum()-budget)>1e-6:raise ValueError('TD budget does not reconcile')
    return output,'COMPONENT_RECONCILED_NOT_MODEL_PROMOTION'
