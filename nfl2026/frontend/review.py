"""Read-only scientific identity and candidate-bound offline release review."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from nfl2026.utilities.io import sha256_file


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def files_digest(folder):
    return {p.name: sha256_file(p) for p in sorted(folder.iterdir()) if p.is_file()}


def verify_hashes(folder, metadata):
    if not metadata.get('file_hashes'):
        raise ValueError('Missing immutable source hashes')
    for name, digest in metadata['file_hashes'].items():
        if Path(name).name != name or sha256_file(folder / name) != digest:
            raise ValueError('Immutable source hash mismatch: ' + name)


def candidate_identity(settings, candidate):
    """Derive identities from validated bytes, including the selected freeze.

    Historic locks are checked against their own archives; only the selected
    current lock is compared with the current global feature state.
    """
    from nfl2026.frontend.v3_release import validate_release, audit_data
    from nfl2026.production.core import verify_lock
    from nfl2026.markets.core import validate_market_snapshot
    from nfl2026.markets.qualification import select_qualifying_values
    from nfl2026.phase7.player_integrity import audit_frozen_players
    from nfl2026.phase7.shadow import verify_shadow

    candidate = Path(candidate)
    manifest = validate_release(candidate, settings=settings)
    current = read(candidate / 'data/current_week.json')
    meta = current['metadata']
    season, week, run_id = int(meta['season']), int(meta['week']), meta['run_id']
    if manifest['prediction_run_id'] != run_id:
        raise ValueError('Candidate prediction identity mismatch')
    run = settings.prediction_root / f'{season}/week_{week:02d}' / run_id
    source = read(run / 'metadata.json')
    if (source['run_id'], source['season'], source['week'], source['lock_id']) != (run_id, season, week, meta['lock_id']):
        raise ValueError('Candidate source scope/lock mismatch')
    verify_hashes(run, source)
    lock = settings.root / f'locks/{season}/week_{week:02d}' / source['lock_id']
    locked = read(lock / 'data_lock.json')
    if verify_lock(lock) or sha256_file(lock/'data_lock.json') != source['file_hashes']['data_lock.json']:
        raise ValueError('Prediction input lock mismatch')
    if (locked['season'], locked['week'], locked['lock_id'], locked['feature_version']) != (season, week, meta['lock_id'], meta['feature_version']):
        raise ValueError('Frozen feature scope mismatch')
    archive = locked.get('exact_feature_archive', {})
    if archive.get('status') != 'READY' or archive['feature_version'] != source['feature_version']:
        raise ValueError('Selected week lacks exact frozen features')
    for name, digest in archive['source_sha256'].items():
        if sha256_file(lock/'inputs'/name) != digest or sha256_file(settings.root/archive['source_paths'][name]) != digest:
            raise ValueError('Selected week numerical feature state changed: ' + name)
    for model in source['model_ids'].values():
        contract = archive['model_contracts'][model['name']]
        if (contract['version'], contract['artifact_sha256']) != (model['version'], model['hash']):
            raise ValueError('Frozen model feature contract mismatch')
    if meta['model_ids'] != source['model_ids']:
        raise ValueError('Displayed model identity mismatch')

    games = current['games']
    selected = {g['game_id']: g.get('prediction_source_run_id') or g.get('prediction_run_id') or run_id for g in games}
    schedule = pd.read_csv(lock/'inputs/games.csv', low_memory=False)
    expected = schedule[schedule.season.eq(season) & schedule.week.eq(week) & schedule.game_type.eq('REG')]
    if len(selected) != len(games) or set(selected) != set(expected.game_id):
        raise ValueError('Candidate fixture population differs from locked schedule')
    for g in games:
        if pd.Timestamp(g['prediction_cutoff_utc']) >= pd.Timestamp(g['kickoff_utc']):
            raise ValueError('Forecast did not precede kickoff')
    freezes = []
    for folder in (settings.artifact_root/f'official_freezes/{season}/week_{week:02d}').glob('*'):
        fm = read(folder/'metadata.json')
        rows = pd.read_csv(folder/'official_predictions.csv')
        mapping = dict(zip(rows.game_id, rows.official_prediction_run_id))
        if (fm['season'], fm['week']) == (season, week) and len(rows) == len(games) and mapping == selected and pd.Timestamp(fm['created_at_utc']) <= pd.Timestamp(manifest['created_at_utc']):
            if fm['freeze_id'] != folder.name or not pd.to_datetime(rows.official_prediction_timestamp, utc=True).lt(pd.to_datetime(rows.kickoff_utc, utc=True)).all():
                raise ValueError('Invalid matching prediction freeze')
            for row in rows.itertuples():
                g = next(g for g in games if g['game_id'] == row.game_id)
                if pd.Timestamp(row.official_prediction_timestamp) != pd.Timestamp(g['prediction_cutoff_utc']):
                    raise ValueError('Freeze cutoff mismatch')
            freezes.append(folder)
    if len(freezes) != 1:
        raise ValueError('Exact candidate prediction freeze missing or ambiguous')
    freeze = freezes[0]

    presentation = read(candidate/'data/player_presentation.json')['players']
    player_counts = {}
    for source_id in set(selected.values()):
        frame, proof, summary = audit_frozen_players(settings, source_id, week, season)
        if summary['identity_or_eligibility_issues']:
            raise ValueError('Selected player source has unresolved eligibility issues')
        for key, value in proof.items():
            if presentation.get(key) != value:
                raise ValueError('Candidate starter evidence differs from frozen source: '+key)
        player_counts[source_id] = len(frame)
    parity = audit_data(settings, candidate)

    value = read(candidate/'data/value.json')
    vm = value['metadata']
    if (vm['season'], vm['week'], vm['prediction_run_id'], vm['prediction_lock_id']) != (season, week, run_id, meta['lock_id']):
        raise ValueError('Value source identity mismatch')
    vr = settings.root/f'value_runs/{season}/week_{week:02d}'/vm['value_run_id']
    authoritative = read(vr/'metadata.json')
    if authoritative['mode'] != 'official' or authoritative['status'] != 'OFFICIAL_VALUE_SNAPSHOT':
        raise ValueError('Value run is not official')
    for key, val in authoritative.items():
        if vm.get(key) != val:
            raise ValueError('Value metadata mismatch: '+key)
    verify_hashes(vr, authoritative)
    if authoritative['prediction_model_ids'] != source['model_ids']:
        raise ValueError('Value model identity mismatch')
    snapshot = settings.root/f'markets/snapshots/{season}/week_{week:02d}'/vm['market_snapshot_id']
    sm = validate_market_snapshot(snapshot, season, week)
    quotes = pd.read_csv(snapshot/'market_quotes.csv')
    if len(quotes) != sm['quote_count'] or len(quotes) != vm['market_quote_count'] or sha256_file(snapshot/'market_quotes.csv') != sha256_file(vr/'market_quotes.csv'):
        raise ValueError('Raw bookmaker quote scope mismatch')
    values = pd.read_csv(vr/'value_bets.csv')
    qualifying = select_qualifying_values(values).sort_values('value_score', ascending=False)
    records = qualifying.replace({float('nan'):None}).to_dict('records')
    if value['values'] != records or len(records) != vm['qualifying_value_count']:
        raise ValueError('Rendered qualifying value entries differ from official run')
    if not pd.read_csv(vr/'qa_report.csv').passed.astype(bool).all():
        raise ValueError('Value QA failed')
    kickoffs = {g['game_id']:g['kickoff_utc'] for g in games}
    if not pd.to_datetime(qualifying.market_timestamp_utc,utc=True).lt(pd.to_datetime(qualifying.game_id.map(kickoffs),utc=True)).all():
        raise ValueError('Post-kickoff bookmaker observation')

    shadows = []
    for path in (settings.artifact_root/f'research/phase7/shadows/{season}/week_{week:02d}').glob('*/manifest.json'):
        sh = read(path)
        if sh.get('official_run_id') == run_id:
            verified = verify_shadow(path.parent)
            if verified['official_lock_id'] != meta['lock_id'] or verified['status'] != 'PROSPECTIVE_SHADOW':
                raise ValueError('Candidate shadow association invalid')
            shadows.append({'path':str(path.relative_to(settings.root)), 'sha256':sha256_file(path)})
    if not shadows:
        raise ValueError('Candidate prospective shadow missing')
    historical_shadows = []
    for path in (settings.artifact_root/f'research/phase7/shadows/{season}').glob('week_*/*/manifest.json'):
        sh = read(path)
        if int(sh['week']) < week:
            verify_shadow(path.parent)
            historical_shadows.append({'path':str(path.relative_to(settings.root)), 'sha256':sha256_file(path)})
    return {'release_id':manifest['release_id'], 'manifest_sha256':sha256_file(candidate/'release_manifest.json'),
        'season':season, 'week':week, 'prediction_run_id':run_id, 'prediction_metadata_sha256':sha256_file(run/'metadata.json'),
        'lock_id':meta['lock_id'], 'lock_sha256':sha256_file(lock/'data_lock.json'),
        'lock_manifest_sha256':sha256_file(lock/'lock_manifest.csv'), 'feature_archive':archive,
        'freeze_id':freeze.name, 'freeze_files':files_digest(freeze),
        'market_snapshot_id':snapshot.name, 'market_metadata_sha256':sha256_file(snapshot/'metadata.json'),
        'market_files':sm['file_hashes'], 'value_run_id':vr.name, 'value_metadata_sha256':sha256_file(vr/'metadata.json'),
        'value_files':authoritative['file_hashes'], 'games':len(games), 'production_players_by_source':player_counts,
        'preview_players':sum(len(g.get('players',[])) for g in games), 'raw_quotes':len(quotes),
        'qualifying_value_entries':len(records), 'historical_bundles':sorted({r['bundle'] for r in parity['games']}),
        'historical_source_runs':sorted({r['source_run_id'] for r in parity['games']}),
        'shadows':shadows, 'historical_shadows':historical_shadows}


def validate_evidence(identity, tests, frontend, browser):
    binding = {k:identity[k] for k in ('release_id','manifest_sha256')}
    for label, evidence in [('Python',tests),('frontend',frontend),('browser',browser)]:
        if any(evidence.get(k) != v for k,v in binding.items()):
            raise ValueError(label+' evidence belongs to another candidate')
    if any(tests.get(k) != 0 for k in ('failed','errors','skipped')) or tests.get('passed',0) <= 0 or not tests.get('network_disabled'):
        raise ValueError('Regression suite not passing offline')
    if frontend.get('failed') != 0 or frontend.get('passed',0) <= 0:
        raise ValueError('Frontend tests failed')
    expected = {(p,w) for p in ('index.html','players.html','value.html','performance.html','model.html') for w in (320,390,768,1280)}
    actual = {(r['page'],r['width']) for r in browser.get('matrix',[]) if r.get('passed')}
    if browser.get('passed') is not True or actual != expected or len(browser.get('matrix',[])) != len(expected) or not all(r.get('passed') for r in browser['matrix']) or not browser.get('interaction_checks_passed'):
        raise ValueError('Complete candidate browser review has not passed')


def finalize(settings):
    from nfl2026.phase7.common import verify_protected, verify_champions, json_write
    selected = read(settings.report_root/'phase75_release_candidate.json')
    candidate = settings.root/selected['path']
    identity = candidate_identity(settings, candidate)
    if (selected['release_id'],selected['manifest_sha256'],selected['source_prediction_run_id']) != (identity['release_id'],identity['manifest_sha256'],identity['prediction_run_id']):
        raise ValueError('Candidate selection identity mismatch')
    r = settings.report_root
    tests, frontend, browser = (read(r/name) for name in ('phase75_tests.json','phase75_frontend_tests.json','phase75_browser.json'))
    baseline = read(r/'phase75_integrity_before.json')
    verify_protected(settings.root, baseline)
    extra = read(r/'phase75_integrity_extra_before.json')
    if not extra:
        raise ValueError('Additional historical/shadow integrity baseline missing')
    for relative, digest in extra.items():
        path = settings.root/relative
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError('Historical release/shadow evidence changed: '+relative)
    # Keep the original production-champion and historical Week 6 sentinel.
    champions = verify_champions(settings)
    evidence = {name:sha256_file(r/name) for name in ('phase75_tests.json','phase75_frontend_tests.json','phase75_browser.json','phase75_integrity_before.json','phase75_integrity_extra_before.json')}
    blockers = []
    try:
        validate_evidence(identity, tests, frontend, browser)
    except ValueError as exc:
        blockers.append(str(exc))
        blockers.extend(f"{case['page']} at {case['width']}px: {case.get('visual_issue','Browser case failed')}"
                        for case in browser.get('matrix',[]) if not case.get('passed'))
    receipt = {'schema':'candidate-review-v2', **{k:identity[k] for k in ('release_id','manifest_sha256')},
        'reviewed_at_utc':datetime.now(timezone.utc).isoformat(), 'identity':identity,'evidence_sha256':evidence,
        'readiness':'BLOCKED' if blockers else 'PASS','remaining_blockers':blockers,
        'tests_passed':tests['passed']+frontend['passed'],
        'tests_failed':sum(tests.get(k,0) for k in ('failed','errors'))+frontend['failed'],
        'python':tests,'frontend_tests':frontend,'browser_sha256':evidence['phase75_browser.json'],
        'champions':champions,'protected_files':len(baseline),'protected_unchanged':True,
        'additional_protected_files':len(extra),
        'public_site_modified':False,'published':False,'model_promotion':False}
    json_write(r/'phase75_release_review.json',receipt)
    if blockers:
        raise ValueError('Release approval BLOCKED: '+'; '.join(blockers))
    return receipt
