"""Immutable weekly evaluation adapter for existing NFL2026 forecasts/sources.

No training, forecast generation, registry writes, bookmaker requests or publishing.
"""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from nfl2026.utilities.io import sha256_file
from nfl2026.production.core import validate_bundle
from nfl2026.production.evaluation import select_evaluation_runs
from nfl2026.research.frozen_evaluation import require_pregame_rows
from nfl2026.research.quarter_phase4 import PBP_COLS
from nfl2026.weekly.metrics import KEYS,team_metrics,player_metrics
from nfl2026.weekly.actuals import final_games,normalize_player_stats,normalize_quarters,snap_identity_matches
from nfl2026.weekly.operations import checkpoint_state,qb_archive_schema,publication_check
from nfl2026.weekly.catalog import evaluate_research

def now():return datetime.now(timezone.utc).isoformat()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def clean(v):
    if isinstance(v,dict):return {str(k):clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [clean(x) for x in v]
    if isinstance(v,np.generic):return clean(v.item())
    if isinstance(v,float) and not np.isfinite(v):return None
    if isinstance(v,(pd.Timestamp,datetime)):return v.isoformat()
    if v is pd.NA or v is pd.NaT:return None
    return v
def write(p,v):
    p=Path(p)
    if p.exists():raise FileExistsError('Immutable weekly artifact exists: '+str(p))
    p.write_text(json.dumps(clean(v),indent=2,ensure_ascii=True,allow_nan=False),encoding='utf-8')
def csv(p,d):
    if Path(p).exists():raise FileExistsError('Immutable weekly CSV exists')
    d.to_csv(p,index=False,lineterminator='\n')

def official_forecasts(settings,season,week,selection_path=None):
    selection=pd.read_csv(selection_path) if selection_path else select_evaluation_runs(settings,season,week)
    if selection.empty or selection.game_id.duplicated().any():raise ValueError('Official per-fixture selection unavailable/duplicated')
    games=[];players=[];identities={};qbs=[]
    for run,subset in selection.groupby('selected_run_id'):
        folder=settings.prediction_root/str(season)/f'week_{week:02d}'/run;meta=read(folder/'metadata.json')
        if meta['run_status']!='OFFICIAL_PRODUCTION' or meta['validation_result']!='PASS' or not meta['deployment_eligible']:raise ValueError('Uncertified official origin')
        if not validate_bundle(folder).passed.all():raise ValueError('Original official bundle drift')
        g=pd.read_csv(folder/'games.csv');g=g[g.game_id.isin(subset.game_id)].copy();p=pd.read_csv(folder/'players.csv');p=p[p.game_id.isin(g.game_id)].copy()
        if set(g.game_id)!=set(subset.game_id):raise ValueError('Selected fixture missing from official run')
        g['forecast_created_at_utc']=meta['created_at_utc'];g['forecast_origin_id']=run;g['original_lock_id']=meta['lock_id'];require_pregame_rows(g,'forecast_created_at_utc')
        p['forecast_origin_id']=run;p['forecast_created_at_utc']=meta['created_at_utc'];p['original_lock_id']=meta['lock_id']
        for frame in [g,p]:
            if frame.duplicated(['game_id'] if frame is g else KEYS).any():raise ValueError('Duplicate selected origin')
        games.append(g);players.append(p)
        for name in ['metadata.json','games.csv','players.csv']:identities[str((folder/name).relative_to(settings.root))]=sha256_file(folder/name)
        lock=settings.root/'locks'/str(season)/f'week_{week:02d}'/meta['lock_id'];qp=lock/'inputs'/f'qb_status_{season}_w{week:02d}.csv'
        if qp.is_file():
            q=pd.read_csv(qp);q=q[q.game_id.isin(subset.game_id)];q['forecast_origin_id']=run;qbs.append(q);identities[str(qp.relative_to(settings.root))]=sha256_file(qp)
    g=pd.concat(games,ignore_index=True);p=pd.concat(players,ignore_index=True)
    if g.game_id.duplicated().any() or p.duplicated(KEYS).any():raise ValueError('Reused origin counted more than once')
    return g,p,selection,pd.concat(qbs,ignore_index=True) if qbs else pd.DataFrame(),identities

def inactive_history(root):
    base=Path(root)/'artifacts/research/availability_checkpoints/2026/week_07'
    names=['inactives-review-20261025T120430Z/validated_final/combined_13_inactive_events.json',
        'main-seven-final-window-review-20261025T165240Z/validated_final/validated_adverse_events.json',
        'den-ari-inactives-review-20261025T183546Z/validated_adverse_events.json',
        'late-afternoon-final-window-review-20261025T201050Z/validated_adverse_events.json',
        'dal-phi-inactives-review-20261026T224746Z/validated_adverse_events.json']
    events=[];sources={}
    for n in names:
        path=base/n
        if not path.is_file():continue
        payload=read(path);items=payload if isinstance(payload,list) else payload.get('events',[])
        for item in items:
            if all(item.get(k) for k in KEYS) and item.get('status') in ['INACTIVE','OUT','IR','WAIVED']:
                raw_name=item.get('source_file')
                if raw_name and (Path(root)/raw_name).is_file():
                    raw_path=Path(root)/raw_name
                    if item.get('source_sha256') and sha256_file(raw_path)!=item['source_sha256']:raise ValueError('Archived inactive raw evidence drift')
                    sources[str(raw_path.relative_to(root))]=sha256_file(raw_path)
                events.append(dict(item,review_source=str(path.relative_to(root)),review_source_sha256=sha256_file(path)))
        sources[str(path.relative_to(root))]=sha256_file(path)
    # The incomplete KC/SEA review is intentionally not retro-certified.
    return pd.DataFrame(events),sources

def operational_audit(root,source,season,week,profile):
    root=Path(root);plan=read(root/profile['checkpoint_plan']) if profile.get('checkpoint_plan') else {'checkpoints':[]};base=root/f'artifacts/research/availability_checkpoints/{season}/week_{week:02d}';checks=[]
    for c in plan['checkpoints']:
        receipts=[read(p) for p in base.glob(c['id']+'-*/receipt.json')]
        checks.append(checkpoint_state(c,receipts,now()))
    schema=qb_archive_schema(pd.read_csv(root/'artifacts/phase2/team_advanced_matrix.csv',nrows=0).columns)
    old=read(root/profile['next_readiness']) if profile.get('next_readiness') else {}
    expected={n:(root/profile['expected_public_release']/n).read_bytes() for n in ['index.html','phase7.css','v31.js','receiver_discovery_v2.js','availability_overlay.js','phase7.js']} if profile.get('expected_public_release') else {}
    observed={n:(source/('public_'+n)).read_bytes() for n in expected if (source/('public_'+n)).is_file()}
    power=root/profile['power_evidence'] if profile.get('power_evidence') else None
    return dict(observed_at_utc=now(),checkpoint_windows=checks,checkpoint_plan_status='OBSERVED_PLAN' if checks else 'UNCONFIGURED_NOT_CERTIFIED',qb_archive_schema=schema,training_inference_parity=old.get('training_inference_parity','UNKNOWN'),
        incomplete_review=dict(path=str(base/'kc-sea-inactives-review-20261025T225030Z'),sealed=False,retroactively_completed=False) if (season,week)==(2026,7) else None,
        sleep_caller_evidence=read(power) if power else None,sleep_attribution='Only exact archived caller evidence is supported; initiating cause and scheduler dispatch cause remain unproven.',
        publishing=publication_check(expected,observed) if expected else dict(status='EXPECTED_DEPLOYMENT_NOT_CONFIGURED'),new_preview_expected_public=False,paid_requests=0,source_requests=read(source/'execution_receipt.json'),
        retired_windows_not_reopened=True,network_retries=0,protected_integrity='CHECKED_SEPARATELY_BEFORE_AND_AFTER',
        missing_participation_is_not_zero=True)

def diagnostic_findings(root,players,labels,joined,games,errors,qbs,participation,inactives):
    findings=[]
    if len(qbs):
        passers=labels[labels.attempts.gt(0)].sort_values('attempts').groupby(['game_id','team']).tail(1)
        for r in qbs.itertuples():
            a=passers[passers.game_id.eq(r.game_id)&passers.team.eq(r.team)]
            if len(a)==1 and str(r.qb_player_id)!=str(a.iloc[0].player_id):
                findings.append(dict(kind='FROZEN_QB_REPORT_DIFFERS_FROM_POSTGAME_LEADING_PASSER',game_id=r.game_id,team=r.team,original_qb_report_id=r.qb_player_id,
                    original_qb_report_name=r.qb_name,postgame_leading_passer_id=a.iloc[0].player_id,postgame_attempts=float(a.iloc[0].attempts),
                    qualification='Outcome diagnostic only. Not pregame starter evidence; does not establish which QB feature the team scorer consumed.'))
    for r in errors.sort_values('score_abs_error',ascending=False).head(5).itertuples():findings.append(dict(kind='LARGE_SCORE_MISS',game_id=r.game_id,team_score_mae=float(r.score_abs_error),home_error=float(r.home_error),away_error=float(r.away_error)))
    frozen_wr=joined[joined.position.eq('WR')];unlisted=frozen_wr[frozen_wr.role_status.ne('EXPECTED_STARTER')&frozen_wr['actual__targets'].ge(5)]
    for r in unlisted.to_dict('records'):findings.append(dict(kind='HIGH_TARGET_FROZEN_NONSTARTER',game_id=r['game_id'],player_id=r['player_id'],team=r['team'],targets=r['actual__targets'],qualification='Missed-opportunity diagnostic, never retrospective starter proof.'))
    if len(inactives):
        lookup=set(tuple(x) for x in inactives[KEYS].to_numpy());active=set(tuple(x) for x in participation.loc[participation.offense_snaps.gt(0),KEYS].to_numpy()) if len(participation) else set()
        for key in sorted(lookup&active):findings.append(dict(kind='INACTIVE_VS_POSITIVE_OFFENSE_SNAP_CONFLICT',game_id=key[0],player_id=key[1],team=key[2],resolution='Preserve both sources; quarantine any activity/eligibility conclusion pending explicit resolution.'))
    return findings

def monitoring_profile(season,week,profile=None):
    if profile is None and (season,week)==(2026,7):
        profile=dict(season=2026,week=7,next_season=2026,next_week=8,checkpoint_plan='reports/week7_public_availability_20261023T140000Z/checkpoint_plan.json',
            next_readiness='reports/week8_phase1_certification_20261027T135222Z/final_outcome.json',fixture_readiness='reports/week8_phase1_certification_20261027T135222Z/fixture_readiness.json',
            historical_trend='reports/team_phase7_20261027T185920Z/validated/official_weeks1_7_metrics.csv',historical_market='reports/value_phase6_20261027T183118Z/validated/historical_value_performance.json',
            expected_public_release='frontend/releases/frontend-v3-1-0aa9d638d7215e81',power_evidence='artifacts/research/availability_checkpoints/2026/week_07/kc-sea-missed-followup-20261026T090455Z/windows_power_caller_evidence.json')
    if not profile or (profile.get('season'),profile.get('week'))!=(season,week):raise ValueError('Explicit exact-season/week monitoring profile required; old readiness must not be carried forward')
    return profile

def run_weekly_evaluation(settings,season,week,source,out,selection_path=None,profile=None):
    root=settings.root;source=Path(source).resolve();out=Path(out).resolve()
    profile=monitoring_profile(season,week,profile)
    if not out.is_relative_to(root/'reports') or not source.is_relative_to(root):raise ValueError('Local append-only research paths required')
    started=now();receipt=read(source/'execution_receipt.json');observed={r['name']:r for r in receipt['attempts'] if r['state']=='CAPTURED'}
    for n in ['games.csv',f'stats_player_week_{season}.csv']:
        if n not in observed or sha256_file(source/n)!=observed[n]['sha256']:raise ValueError('Verified completed source missing: '+n)
    schedule=pd.read_csv(source/'games.csv',low_memory=False);games=final_games(schedule,season,week)
    official,players,selection,qbs,sources=official_forecasts(settings,season,week,selection_path)
    if set(official.game_id)!=set(games.game_id):raise ValueError('Official/FINAL fixture population mismatch')
    raw=pd.read_csv(source/f'stats_player_week_{season}.csv',low_memory=False);labels,invalid=normalize_player_stats(raw,games,season,week,observed[f'stats_player_week_{season}.csv'])
    pbp_path=root/f'NFL Fantasy/{season} Stats/pbp_{season}.parquet';schema=pq.read_schema(pbp_path).names
    extra=[c for c in ['passer_player_id','rusher_player_id','receiver_player_id','no_play'] if c in schema]
    pbp=pq.read_table(pbp_path,columns=list(dict.fromkeys(PBP_COLS+extra)),filters=[('week','=',week)]).to_pandas();pbp=pbp[pbp.season.eq(season)&pbp.season_type.eq('REG')]
    quarters=normalize_quarters(pbp,games,sha256_file(pbp_path),now());sources[str(pbp_path.relative_to(root))]=sha256_file(pbp_path)
    team,errors,bins,bias=team_metrics(official,games);pm,joined,calibration=player_metrics(players,labels)
    snaps_path=source/f'snap_counts_{season}.csv';participation=pd.DataFrame();snap_audit=dict(status='UNOBSERVED',source_rows=0,matched_rows=0)
    if snaps_path.is_file():
        snap=pd.read_csv(snaps_path);snap=snap[snap.season.eq(season)&snap.week.eq(week)]
        parts=[];audits=[]
        for lock,scope in official.groupby('original_lock_id'):
            roster_path=root/'locks'/str(season)/f'week_{week:02d}'/lock/'inputs'/f'weekly_roster_{season}.csv'
            roster=pd.read_csv(roster_path,low_memory=False);roster=roster[roster.season.eq(season)&roster.week.eq(week)]
            if roster.empty:raise ValueError('Original lock lacks exact-week roster identity crosswalk')
            found,audit=snap_identity_matches(snap[snap.game_id.isin(scope.game_id)],roster,games[games.game_id.isin(scope.game_id)])
            found['crosswalk_source_path']=str(roster_path.relative_to(root));found['crosswalk_source_sha256']=sha256_file(roster_path)
            parts.append(found);audits.append(dict(lock_id=lock,**audit));sources[str(roster_path.relative_to(root))]=sha256_file(roster_path)
        participation=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
        snap_audit=dict(status='FROZEN_EXACT_WEEK_ID_CROSSWALK',source_rows=sum(a['source_rows'] for a in audits),matched_rows=len(participation),unresolved_rows=sum(a.get('unresolved_rows',0) for a in audits),locks=audits,
            current_mutable_roster_not_used=True,absence_does_not_establish_nonparticipation=True,zero_snaps_do_not_establish_zero_stat_outcomes=True)
        if len(participation):
            participation['participation_source_sha256']=sha256_file(snaps_path);participation['participation_observed_at_utc']=observed[snaps_path.name]['observed_at_utc'];participation['not_a_zero_stat_label']=True
    inactive,inactive_sources=inactive_history(root) if (season,week)==(2026,7) else (pd.DataFrame(),{});sources.update(inactive_sources)
    research,tables,research_sources=evaluate_research(root,players,official,labels,games,quarters) if season==2026 and week==7 else ([],{},{});sources.update(research_sources)
    if profile.get('challengers'):
        from nfl2026.weekly.submissions import evaluate_submissions
        additional,extra_sources=evaluate_submissions(root,profile['challengers'],games,official,labels,quarters);research.extend(additional);sources.update(extra_sources)
    for p in source.iterdir():
        if p.is_file():sources[str(p.relative_to(root))]=sha256_file(p)
    dependency_paths=[root/profile[k] for k in ['next_readiness','fixture_readiness','historical_trend','historical_market','checkpoint_plan','power_evidence'] if profile.get(k)]
    for p in dependency_paths+[root/'artifacts/phase2/team_advanced_matrix.csv']:
        sources[str(p.relative_to(root))]=sha256_file(p)
    if profile.get('expected_public_release'):
        for n in ['index.html','phase7.css','v31.js','receiver_discovery_v2.js','availability_overlay.js','phase7.js']:
            p=root/profile['expected_public_release']/n;sources[str(p.relative_to(root))]=sha256_file(p)
    old_stats_path=root/f'data/postgame_downloads/{season}/week_{week:02d}/20261027T131016603218Z/stats_player_week_{season}.csv'
    revisions=[]
    if season==2026 and week==7 and old_stats_path.is_file():
        old_raw=pd.read_csv(old_stats_path,low_memory=False);old_labels,_=normalize_player_stats(old_raw,games,season,week,dict(url='ARCHIVED_PRIOR_DOWNLOAD',sha256=sha256_file(old_stats_path),observed_at_utc='2026-10-27T13:10:16.974469+00:00'))
        compare=labels.merge(old_labels,on=KEYS,how='outer',suffixes=('_new','_prior'),validate='one_to_one',indicator=True)
        for target in ['passing_yards','attempts','carries','rushing_yards','targets','receptions','receiving_yards','anytime_offensive_td']:
            a=pd.to_numeric(compare[target+'_new'],errors='coerce');b=pd.to_numeric(compare[target+'_prior'],errors='coerce')
            different=~(np.isclose(a,b,equal_nan=True))
            for i in compare.index[different]:revisions.append(dict(**{k:compare.loc[i,k] for k in KEYS},target=target,previous_label=b.loc[i],current_label=a.loc[i],resolution='EXPLICIT_NEW_SOURCE_VALUE_NOT_IMPLICIT_ZERO'))
        sources[str(old_stats_path.relative_to(root))]=sha256_file(old_stats_path)
    if selection_path:sources[str(Path(selection_path).resolve().relative_to(root))]=sha256_file(Path(selection_path))
    base=root/f'artifacts/research/availability_checkpoints/{season}/week_{week:02d}';plan=read(root/profile['checkpoint_plan']) if profile.get('checkpoint_plan') else {'checkpoints':[]}
    for c in plan['checkpoints']:
        for p in base.glob(c['id']+'-*/receipt.json'):sources[str(p.relative_to(root))]=sha256_file(p)
    code={str(p.relative_to(root)):sha256_file(p) for p in (root/'nfl2026/weekly').glob('*.py')}
    identity=hashlib.sha256(json.dumps(dict(season=season,week=week,profile=profile,sources=sources,code=code,selection=selection[['game_id','selected_run_id']].to_dict('records')),sort_keys=True).encode()).hexdigest()
    run=out/'evaluations'/('weekly-eval-'+identity[:20])
    if run.exists():
        if not (run/'completed_manifest.json').is_file():raise FileExistsError('Incomplete prior attempt preserved: '+str(run))
        seal=read(run/'completed_manifest.json')
        for n,h in seal['files'].items():
            if sha256_file(run/n)!=h:raise ValueError('Immutable weekly evaluation drift')
        return dict(status='REUSED_IMMUTABLE_EVALUATION',path=str(run),evaluation_id='weekly-eval-'+identity[:20],operational_observation='HISTORICAL_NOT_A_NEW_MONITORING_EXECUTION',original_execution=read(run/'execution_receipt.json'))
    run.mkdir(parents=True);actual_id='weekly-actuals-'+hashlib.sha256((observed['games.csv']['sha256']+observed[f'stats_player_week_{season}.csv']['sha256']+sha256_file(pbp_path)).encode()).hexdigest()[:20]
    csv(run/'team_actuals.csv',games);csv(run/'player_actuals.csv',labels);csv(run/'quarter_actuals.csv',quarters);csv(run/'quarantined_unidentified_stats.csv',invalid)
    csv(run/'official_fixture_selection.csv',selection);csv(run/'official_players.csv',players);csv(run/'team_fixture_errors.csv',errors);csv(run/'player_label_join.csv',joined);csv(run/'player_metrics.csv',pm);csv(run/'player_calibration.csv',calibration)
    if len(participation):csv(run/'verified_snap_identities.csv',participation)
    if len(participation):
        coverage=players[KEYS+['player_name','position','role_status']].merge(participation[KEYS+['offense_snaps','defense_snaps','st_snaps','participation_source_sha256','crosswalk_source_sha256']],on=KEYS,how='left',validate='one_to_one')
        coverage['participation_state']=np.where(coverage.offense_snaps.gt(0),'EXPLICIT_POSITIVE_OFFENSE_SNAPS',np.where(coverage.offense_snaps.eq(0),'EXPLICIT_ZERO_OFFENSE_SNAPS_NOT_ZERO_STATS','UNKNOWN'))
        csv(run/'frozen_player_participation_coverage.csv',coverage)
    csv(run/'source_label_revisions.csv',pd.DataFrame(revisions,columns=KEYS+['target','previous_label','current_label','resolution']))
    if len(inactive):csv(run/'archived_verified_inactive_evidence.csv',inactive)
    for name,frame in tables.items():csv(run/(name+'.csv'),frame)
    findings=diagnostic_findings(root,players,labels,joined,games,errors,qbs,participation,inactive)
    write(run/'team_metrics.json',dict(metrics=team,calibration=bins,bias_groups=bias));write(run/'research_comparisons.json',research);write(run/'improvement_findings.json',findings)
    write(run/'participation_coverage.json',snap_audit)
    operations=operational_audit(root,source,season,week,profile);write(run/'operational_readiness.json',operations)
    readiness=read(root/profile['next_readiness']) if profile.get('next_readiness') else dict(status='NOT_CONFIGURED_NO_CERTIFICATION',official_certified_fixtures=0)
    fixture_readiness=read(root/profile['fixture_readiness']) if profile.get('fixture_readiness') else dict(fixtures=[],source_observation_utc=None)
    if profile.get('next_season') and profile.get('next_week'):
        prefix=f"{profile['next_season']}_{profile['next_week']:02d}_"
        if any(not r['game_id'].startswith(prefix) for r in fixture_readiness['fixtures']):raise ValueError('Next-week readiness fixture population belongs to another week')
    readiness.update(phase9_observed_at_utc=now(),source_certification_refreshed=False,source_certification_as_of_utc=fixture_readiness.get('source_observation_utc'),
        benchmark_label_policy='Unmatched frozen labels remain UNKNOWN; no synthetic zero outcomes',forecast_generated=False,automatic_retraining=False,automatic_promotion=False)
    write(run/'next_week_readiness.json',readiness);write(run/'next_week_fixture_source_readiness.json',fixture_readiness)
    if (season,week)==(2026,7):write(run/'week8_readiness.json',readiness);write(run/'week8_fixture_source_readiness.json',fixture_readiness)
    csv(run/'official_weekly_trend.csv',pd.read_csv(root/profile['historical_trend']) if profile.get('historical_trend') else pd.DataFrame([dict(period=f'{season}_W{week}',**team)]))
    write(run/'historical_market_evaluation.json',read(root/profile['historical_market']) if profile.get('historical_market') else dict(status='NO_PINNED_HISTORICAL_MARKET_REPORT'))
    write(run/'monitoring_profile.json',profile)
    summary=dict(status='COMPLETE_EVALUATION_WITH_EXPLICIT_GAPS',evaluation_id='weekly-eval-'+identity[:20],actual_snapshot_id=actual_id,season=season,week=week,
        final_fixtures=len(games),frozen_player_forecasts=len(players),matched_player_identities=int(joined.label_match.eq('both').sum()),unknown_player_identities=int(joined.label_match.ne('both').sum()),
        quarter_team_rows=len(quarters),official_passing_predictions=int(pd.to_numeric(players['expected_pass_yards'],errors='coerce').notna().sum()) if 'expected_pass_yards' in players else 0,official_quarter_predictions=0,automatic_training=False,promotion=False,paid_requests=0,publication=False,
        original_run_ids=sorted(official.forecast_origin_id.unique()),source_hashes=sources,implementation_hashes=code)
    write(run/'summary.json',summary);write(run/'execution_receipt.json',dict(started_at_utc=started,completed_at_utc=now(),status='COMPLETE_EVALUATION_WITH_EXPLICIT_GAPS',source_network_requests=0,inference_executed=False,new_forecasts=False,scheduled_time_used_as_proof=False))
    implementation=run/'implementation';implementation.mkdir()
    for n in code:(implementation/Path(n).name).write_bytes((root/n).read_bytes())
    files={str(p.relative_to(run)):sha256_file(p) for p in run.rglob('*') if p.is_file()};write(run/'completed_manifest.json',dict(evaluation_id=summary['evaluation_id'],files=files,external_files=sources,implementation_hashes=code))
    return dict(status=summary['status'],path=str(run),evaluation_id=summary['evaluation_id'],actual_snapshot_id=actual_id,team_metrics=team,matched_players=summary['matched_player_identities'],unknown_players=summary['unknown_player_identities'])
