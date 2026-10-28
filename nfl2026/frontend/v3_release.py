"""Immutable V3 release candidates. No official build pointer or deployment writes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from nfl2026.deployment import validate_frontend_dist
from nfl2026.phase7.common import json_write
from nfl2026.phase7.preview import build_preview, verify_authoritative_bundle
from nfl2026.utilities.io import sha256_file

UI_FILES = ("index.html", "players.html", "value.html", "performance.html", "model.html", "phase7.js", "boards.js", "phase7.css")
RESULT_FIELDS = {"result_status", "actual_home_score", "actual_away_score", "result_source"}


def digest_files(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def audit_data(settings, candidate):
    """Every frozen field and final score, not just representative cards."""
    from nfl2026.postgame import _completion_mask, inspect_completed_week_actuals
    original = settings.root / "frontend/dist"
    rows = []
    schedule = pd.read_csv(settings.root / "data/phase2_external/games.csv", low_memory=False)
    verified_weeks = {}
    for path in sorted((original / "data").glob("*.json")):
        if path.name != "current_week.json" and not path.name.startswith("week_"):
            if sha256_file(path) != sha256_file(candidate / "data" / path.name):
                raise ValueError(f"Original dashboard payload changed: {path.name}")
            continue
        before = json.loads(path.read_text())
        after = json.loads((candidate / "data" / path.name).read_text())
        if before["metadata"] != after["metadata"] or len(before["games"]) != len(after["games"]):
            raise ValueError("Frontend metadata/game population changed")
        audit = verify_authoritative_bundle(settings, after)
        for old, new, proof in zip(before["games"], after["games"], audit):
            for field, value in old.items():
                if field not in RESULT_FIELDS and new.get(field) != value:
                    raise ValueError(f"Frozen frontend field changed: {old['game_id']}/{field}")
            if new.get("result_status") == "FINAL":
                season, week = int(new["season"]), int(new["week"])
                key = (season, week)
                if key not in verified_weeks:
                    expected = schedule[schedule.season.eq(season) & schedule.week.eq(week) & schedule.game_type.eq("REG")]
                    if not _completion_mask(expected).all() or inspect_completed_week_actuals(settings, season, week, expected)["status"] != "ALREADY_CURRENT":
                        raise ValueError("Unvalidated result annotation")
                    verified_weeks[key] = expected.set_index("game_id")
                actual = verified_weeks[key].loc[new["game_id"]]
                if (new["actual_home_score"],new["actual_away_score"]) != (actual.home_score,actual.away_score):
                    raise ValueError("Displayed final result differs from validated source")
            rows.append({"bundle":path.name, **proof, "all_fields_equal":True,
                "players":len(new.get("players",[])),"results_separate":True})
    # Archived values remain exact bytes; verify their original immutable run.
    value = json.loads((candidate / "data/value.json").read_text())
    meta = value["metadata"]
    if meta.get("value_run_id"):
        directory = settings.root / f"value_runs/{meta['season']}/week_{int(meta['week']):02d}" / meta["value_run_id"]
        for name, digest in meta["file_hashes"].items():
            if sha256_file(directory / name) != digest:
                raise ValueError("Authoritative value snapshot hash failure")
        from nfl2026.frontend.build import build_market_game_context
        values = pd.read_csv(directory / "value_bets.csv")
        current = json.loads((candidate / "data/current_week.json").read_text())
        if meta.get('prediction_run_id') != current['metadata']['run_id']:
            raise ValueError('Value snapshot anchor differs from the displayed forecast')
        selected={game['game_id']:game.get('prediction_source_run_id') or game.get('prediction_run_id') or current['metadata']['run_id'] for game in current['games']}
        for quote in values.to_dict('records'):
            if quote.get('game_id') in selected:
                if quote.get('prediction_run_id') != selected[quote['game_id']]:
                    raise ValueError('Value quote differs from selected fixture prediction')
                if quote.get('prediction_anchor_run_id',meta['prediction_run_id']) != meta['prediction_run_id']:
                    raise ValueError('Value quote anchor differs from its immutable value run')
        from nfl2026.markets.core import validate_market_snapshot
        snapshot=settings.root/'markets/snapshots'/str(meta['season'])/f"week_{int(meta['week']):02d}"/meta['market_snapshot_id']
        validate_market_snapshot(snapshot,int(meta['season']),int(meta['week']))
        context = build_market_game_context(values,pd.DataFrame(current["games"]))
        if context != value.get("market_game_context",{}):
            raise ValueError("Market-implied comparison differs from frozen prices")
    performance_path = candidate/'data/phase7_performance.json'
    if performance_path.is_file():
        performance = json.loads(performance_path.read_text())
        if performance.get('schema') == 'official-performance-presentation-v1':
            from nfl2026.frontend.performance import build_performance_presentation
            if performance != build_performance_presentation(settings):
                raise ValueError('Official performance presentation differs from verified evaluation sources')
    return {"status":"PASS","bundles":len(set(r['bundle'] for r in rows)),"games":rows,
        "current_week_byte_identical":sha256_file(original/'data/current_week.json')==sha256_file(candidate/'data/current_week.json'),
        "value_byte_identical":sha256_file(original/'data/value.json')==sha256_file(candidate/'data/value.json'),
        "historical_results_only_from_validated_actuals":True,
        "market_snapshot_id":meta.get('market_snapshot_id'),"value_run_id":meta.get('value_run_id'),
        "fixture_market_lineage_verified":True}


def validate_release(candidate, *, settings=None, require_current=True):
    """Fail closed on corruption, additions, research forecasts or stale candidates."""
    candidate = Path(candidate).resolve()
    manifest = json.loads((candidate / "release_manifest.json").read_text())
    files = {str(p.relative_to(candidate)).replace('\\','/'):sha256_file(p) for p in candidate.rglob('*')
             if p.is_file() and p.name != 'release_manifest.json'}
    if files != manifest['files']:
        raise ValueError("Release candidate files changed or unexpected files added")
    if digest_files({k:v for k,v in files.items() if k!='data/frontend_release.json'}) != manifest['content_sha256']:
        raise ValueError("Release content identity mismatch")
    prefix='frontend-v3-1-' if manifest.get('frontend_version')=='3.1' else 'frontend-v3-'
    if manifest['release_id']!=prefix+manifest['content_sha256'][:16]:
        raise ValueError("Release ID is not content-derived")
    identity = json.loads((candidate/'data/frontend_release.json').read_text())
    if identity['release_id']!=manifest['release_id'] or identity['prediction_run_id']!=manifest['prediction_run_id']:
        raise ValueError("Release identity mismatch")
    if 'data/current_week.json' not in identity.get('data_sha256',{}):
        raise ValueError("Release lacks exact public forecast/data verification contract")
    for name,digest in {**identity['ui_sha256'],**identity['data_sha256']}.items():
        if files.get(name)!=digest:raise ValueError("Public verification contract differs from candidate bytes")
    errors = validate_frontend_dist(candidate)
    for name in UI_FILES:
        if not (candidate/name).is_file():errors.append('missing V3 '+name)
    for name in UI_FILES[:5]:
        html=(candidate/name).read_text(encoding='utf-8')
        if 'src="app.js"' in html or 'href="styles.css"' in html:errors.append('legacy page not integrated: '+name)
        if not all(token in html for token in ('lang="en"','id="main"','aria-label="Primary"')):errors.append('missing accessibility structure: '+name)
    if errors:raise ValueError('; '.join(errors))
    current = json.loads((candidate/'data/current_week.json').read_text())
    if manifest.get('availability_overlay_schema'):
        from nfl2026.frontend.availability import validate_overlay
        overlay=json.loads((candidate/'data/current_availability.json').read_text())
        validate_overlay(overlay,current,now=datetime.now(timezone.utc),require_fresh=require_current)
        if overlay.get('forecast_payload_sha256')!=files['data/current_week.json']:raise ValueError('Availability forecast hash mismatch')
        for name in UI_FILES[:5]:
            page=(candidate/name).read_text(encoding='utf-8')
            if f'"availability_overlay.js?v={files["availability_overlay.js"][:16]}"' not in page:raise ValueError('Availability safety asset missing/fingerprint differs')
    if current['metadata'].get('run_status')!='OFFICIAL_PRODUCTION' or not current['metadata'].get('deployment_eligible'):
        raise ValueError("Candidate forecast is not official deployment-eligible production")
    if any(p.suffix in {'.joblib','.csv','.parquet'} for p in candidate.rglob('*')):
        raise ValueError("Research/raw prediction artifacts cannot enter a public release")
    if manifest.get('frontend_version')=='3.1':
        if not (candidate/'v31.js').is_file():raise ValueError('V3.1 props implementation missing')
        if 'data-frontend-version="3.1"' not in (candidate/'index.html').read_text(encoding='utf-8'):raise ValueError('V3.1 must fail closed on missing Props module')
        for name in UI_FILES[:5]:
            html=(candidate/name).read_text(encoding='utf-8')
            for asset in ('phase7.css','phase7.js','boards.js','v31.js'):
                if f'"{asset}' in html and f'"{asset}?v={files[asset][:16]}"' not in html:raise ValueError('V3.1 asset cache fingerprint missing/mismatched: '+asset)
        passing=json.loads((candidate/'data/passing_projections.json').read_text())
        if passing.get('schema')!='certified-passing-v1' or passing.get('status')!='NOT_CERTIFIED' or passing.get('predictions'):
            raise ValueError('Passing is not certified; research output must not enter public Props')
        from nfl2026.frontend.receiver_discovery import validate_release_contract
        validate_release_contract(settings,candidate,manifest)
    if settings is not None and require_current:
        from nfl2026.production.partial_gate import require_certified_partial_frontend
        require_certified_partial_frontend(settings,candidate)
        if sha256_file(candidate/'data/current_week.json')!=sha256_file(settings.root/'frontend/dist/data/current_week.json'):
            raise ValueError("Candidate no longer matches current official forecast; rebuild/review required")
        audit_data(settings,candidate)
    return manifest


def build_release(settings, *, version='3'):
    if version not in {'3','3.1'}:raise ValueError('Unknown frontend release version')
    build_preview(settings)
    preview = settings.root/'frontend/phase7_preview'
    # Build in an owned new directory; no global/dist/published pointer is moved.
    import tempfile
    parent=settings.root/'frontend/releases';parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.v3-staging-',dir=parent) as temporary:
        staging=Path(temporary)
        shutil.copytree(preview,staging,dirs_exist_ok=True)
        for name in UI_FILES[:5]:
            text=(staging/name).read_text(encoding='utf-8').replace('LOCAL PREVIEW · NOT PUBLISHED','FRONTEND V3 · FROZEN FORECASTS')
            text=text.replace('Phase 7 design preview','Frontend V3').replace('LOCAL PREVIEW','FRONTEND V3')
            (staging/name).write_text(text,encoding='utf-8')
        if version=='3.1':
            from nfl2026.phase7.player_integrity import write_current_audit, audit_frozen_players
            proof,summary=write_current_audit(settings)
            exclusions=[]
            runs={}
            for path in (staging/'data').glob('week_*.json'):
                bundle=json.loads(path.read_text())
                for game in bundle['games']:
                    source=game.get('prediction_source_run_id') or game.get('prediction_run_id') or bundle['metadata']['run_id']
                    runs.setdefault((source,int(game['week'])),set()).add(str(game['game_id']))
            for (run,week),game_ids in runs.items():
                try:
                    _,historical,_=audit_frozen_players(settings,run,week)
                    proof.update({key:value for key,value in historical.items() if key.split('|',1)[0] in game_ids})
                except (ValueError,FileNotFoundError,KeyError) as exc:
                    exclusions.append({'run':run,'state':'STARTER_EVIDENCE_UNAVAILABLE','reason':str(exc)})
            json_write(staging/'data/player_presentation.json',{'schema':'frozen-starter-presentation-v1','players':proof,'historical_exclusions':exclusions,'current_audit':summary})
            json_write(staging/'data/passing_projections.json',{'schema':'certified-passing-v1','status':'NOT_CERTIFIED','predictions':[]})
            shutil.copyfile(settings.root/'frontend/phase7_src/v31.js',staging/'v31.js')
            index=(staging/'index.html').read_text(encoding='utf-8').replace('<script src="phase7.js">','<script src="v31.js"></script><script src="phase7.js">')
            index=index.replace('<body data-page="games">','<body data-page="games" data-frontend-version="3.1">')
            (staging/'index.html').write_text(index,encoding='utf-8')
            for name in UI_FILES[:5]:
                html=(staging/name).read_text(encoding='utf-8').replace('FRONTEND V3 ·','FRONTEND V3.1 ·').replace('Frontend V3 ·','Frontend V3.1 ·')
                for asset in ('phase7.css','phase7.js','boards.js','v31.js'):
                    html=html.replace(f'"{asset}"',f'"{asset}?v={sha256_file(staging/asset)[:16]}"')
                (staging/name).write_text(html,encoding='utf-8')
        json_write(staging/'data/frontend_contract.json',{'schema':'frontend-v3-release-v2','forecast_authority':'original immutable production runs',
            'new_models_promoted':False,'public_verification':'exact run ID plus frontend release ID and UI/data hashes'})
        parity = audit_data(settings,staging)
        data=json.loads((staging/'data/current_week.json').read_text())
        files={str(p.relative_to(staging)).replace('\\','/'):sha256_file(p) for p in staging.rglob('*') if p.is_file()}
        digest=digest_files(files);release_id=('frontend-v3-1-' if version=='3.1' else 'frontend-v3-')+digest[:16]
        identity={'release_id':release_id,'prediction_run_id':data['metadata']['run_id'],
            'ui_sha256':{name:files[name] for name in (*UI_FILES, *(('v31.js',) if version=='3.1' else ()))},
            'data_sha256':{name:digest for name,digest in files.items() if name.startswith('data/')}}
        json_write(staging/'data/frontend_release.json',identity)
        manifest={'status':'VALIDATED_RELEASE_CANDIDATE_NOT_PUBLISHED','release_id':release_id,'content_sha256':digest,
            'prediction_run_id':data['metadata']['run_id'],'created_at_utc':datetime.now(timezone.utc).isoformat(),
            'files':{str(p.relative_to(staging)).replace('\\','/'):sha256_file(p) for p in staging.rglob('*') if p.is_file()},
            'data_parity':parity,'research_forecasts_included':False,'official_pointer_updated':False}
        if version=='3.1':manifest['frontend_version']='3.1'
        json_write(staging/'release_manifest.json',manifest)
        validate_release(staging,settings=settings)
        destination=parent/release_id
        if destination.exists():
            validate_release(destination,settings=settings)
        else:
            shutil.copytree(staging,destination)
    json_write(settings.report_root/('phase75_release_candidate.json' if version=='3.1' else 'phase7e_release_candidate.json'),{'path':str(destination.relative_to(settings.root)),
        'release_id':release_id,'manifest_sha256':sha256_file(destination/'release_manifest.json'),
        'source_prediction_run_id':data['metadata']['run_id'],'data_parity':'PASS'})
    pd.DataFrame(parity['games']).to_csv(settings.report_root/('phase75_data_parity.csv' if version=='3.1' else 'phase7e_data_parity.csv'),index=False)
    return destination


def validate_publication_review(settings, candidate):
    """Offline QA is bound to this exact immutable candidate, never another draft."""
    manifest=json.loads((Path(candidate)/'release_manifest.json').read_text(encoding='utf-8'))
    path=settings.report_root/('phase75_release_review.json' if manifest.get('frontend_version')=='3.1' else 'phase7e_release_review.json')
    if not path.exists():raise ValueError("V3 release has no completed release-review receipt")
    review=json.loads(path.read_text(encoding='utf-8'))
    manifest=json.loads((Path(candidate)/'release_manifest.json').read_text(encoding='utf-8'))
    if (review.get('release_id')!=manifest['release_id'] or review.get('manifest_sha256')!=sha256_file(Path(candidate)/'release_manifest.json')
        or review.get('readiness')!='PASS' or review.get('remaining_blockers') or review.get('tests_failed')!=0):
        raise ValueError("Release review is missing, failed, or belongs to another immutable candidate")
    if manifest.get('frontend_version') == '3.1':
        from nfl2026.frontend.review import candidate_identity, validate_evidence, read
        if review.get('schema') != 'candidate-review-v2' or review.get('identity') != candidate_identity(settings, Path(candidate)):
            raise ValueError('Release review scientific/bookmaker identity is missing or mismatched')
        for name, digest in review.get('evidence_sha256', {}).items():
            if Path(name).name != name or sha256_file(settings.report_root/name) != digest:
                raise ValueError('Release review evidence changed')
        required = ('phase75_tests.json','phase75_frontend_tests.json','phase75_browser.json','phase75_integrity_before.json','phase75_integrity_extra_before.json')
        if set(review.get('evidence_sha256',{})) != set(required):
            raise ValueError('Release review evidence incomplete')
        validate_evidence(review['identity'], *(read(settings.report_root/name) for name in required[:3]))
    return review
