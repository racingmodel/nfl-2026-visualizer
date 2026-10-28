"""A dated eligibility overlay; frozen forecasts and probabilities are read-only."""
from datetime import datetime,timezone
import json,re,shutil
from pathlib import Path
import pandas as pd
from nfl2026.utilities.io import sha256_file

SCHEMA='current-availability-v1'
STATES={'UNAVAILABLE','UNKNOWN','UNCERTAIN','EXPECTED_ELIGIBLE','CLOSED'}

def validate_overlay(overlay,current,*,now=None,require_fresh=False):
    if overlay.get('schema')!=SCHEMA:raise ValueError('Unknown availability schema')
    meta=current['metadata'];run=meta['run_id']
    if (overlay.get('season'),overlay.get('week'),overlay.get('prediction_run_id'))!=(meta['season'],meta['week'],run):raise ValueError('Availability prediction scope differs')
    stamp=pd.Timestamp(overlay['created_at_utc']);expires=pd.Timestamp(overlay['valid_until_utc'])
    if stamp.tzinfo is None or expires.tzinfo is None or not stamp<expires:raise ValueError('Availability observation/expiry invalid')
    if now is not None and stamp>pd.Timestamp(now):raise ValueError('Future availability overlay')
    if require_fresh and (now is None or pd.Timestamp(now)>=expires):raise ValueError('Availability overlay expired; refresh and review required')
    fixtures={g['game_id']:g for g in current['games']}
    if set(overlay['fixtures'])!=set(fixtures):raise ValueError('Availability fixture population differs')
    expected={}
    for game in current['games']:
        f=overlay['fixtures'][game['game_id']]
        if f['away_team']!=game['away_team'] or f['home_team']!=game['home_team']:raise ValueError('Availability fixture teams differ')
        if pd.Timestamp(f['cutoff_utc'])!=pd.Timestamp(game['kickoff_utc'])-pd.Timedelta(minutes=1):raise ValueError('Availability cutoff differs')
        result=f.get('final_result')
        if result is not None:
            if f.get('state')!='FINAL' or result.get('status')!='FINAL':raise ValueError('Only final fixtures may display final results')
            if any(type(result.get(k)) is not int or result[k]<0 for k in ['away_points','home_points']):raise ValueError('Final result scores invalid')
            if not re.fullmatch('[0-9a-f]{64}',result.get('source_manifest_sha256','')):raise ValueError('Final result provenance missing')
            observed=pd.Timestamp(result['observed_at_utc'])
            if observed.tzinfo is None or not pd.Timestamp(game['kickoff_utc'])<observed<=stamp:raise ValueError('Final result observation invalid')
        for p in game['players']:
            key=f"{game['game_id']}|{p['player_id']}|{p['team']}"
            if key in expected:raise ValueError('Duplicate frozen player identity')
            expected[key]=(game,p)
    if set(overlay['players'])!=set(expected):raise ValueError('Availability player population differs')
    for key,row in overlay['players'].items():
        game,p=expected[key]
        if (row['game_id'],row['player_id'],row['team'])!=(game['game_id'],p['player_id'],p['team']):raise ValueError('Availability player identity differs')
        if row['prediction_source_run_id']!=(game.get('prediction_source_run_id') or game.get('prediction_run_id') or run):raise ValueError('Availability player source run differs')
        if row['state'] not in STATES:raise ValueError('Availability state unsupported')
        if any(k.startswith(('expected_','research_','original_')) or k in {'model_probability','reconciled_td_probability','book_odds'} for k in row):raise ValueError('Overlay may not contain revised forecast/price fields')
    for source in overlay['sources']:
        observed=pd.Timestamp(source['observed_at_utc'])
        if observed.tzinfo is None or observed>stamp or not re.fullmatch('[0-9a-f]{64}',source['sha256']):raise ValueError('Availability source time/hash invalid')
    return {'status':'PASS','games':len(fixtures),'players':len(expected),'forecast_fields_modified':False}

def build_availability_release(settings,source,overlay_path,output):
    from nfl2026.frontend.v3_release import validate_release,audit_data,digest_files,UI_FILES
    source=Path(source).resolve();dest=Path(output).resolve()
    if not dest.is_relative_to(settings.root/'reports'):raise ValueError('Availability candidate must be in an owned report directory')
    if dest.exists():raise FileExistsError('Candidate is immutable')
    base=validate_release(source,settings=settings)
    overlay=json.loads(Path(overlay_path).read_text());current=json.loads((source/'data/current_week.json').read_text())
    validate_overlay(overlay,current,now=datetime.now(timezone.utc),require_fresh=True)
    if overlay['forecast_payload_sha256']!=sha256_file(source/'data/current_week.json'):raise ValueError('Availability frozen forecast hash differs')
    shutil.copytree(source,dest)
    shutil.copy2(overlay_path,dest/'data/current_availability.json')
    shutil.copy2(settings.root/'frontend/phase7_src/availability_overlay.js',dest/'availability_overlay.js')
    # Await the overlay before any current selection can render, including
    # missing/failed overlay fetches. No timer or post-render safety patch.
    phase=(dest/'phase7.js').read_text(encoding='utf-8')
    phase=phase.replace('async function games(){','async function games(){\n await loadAvailabilityOverlay();',1)
    phase=phase.replace('async function performance(){','async function performance(){\n await loadAvailabilityOverlay();',1)
    (dest/'phase7.js').write_text(phase,encoding='utf-8')
    boards=(dest/'boards.js').read_text(encoding='utf-8')
    for name in ['playersBoard','marketBoard','modelBoard']:
        marker='async function '+name+'(){'
        if marker not in boards:raise ValueError('Availability render entry point missing: '+name)
        boards=boards.replace(marker,marker+'await loadAvailabilityOverlay();',1)
    (dest/'boards.js').write_text(boards,encoding='utf-8')
    css=(dest/'phase7.css').read_text(encoding='utf-8')+'\n.availability-notice,.availability-status{overflow-wrap:anywhere}.availability-notice{border:1px solid #d6ac52;border-radius:12px;padding:1rem;margin:1rem 0;background:#152331}.availability-reference{margin:.6rem 0;padding:.6rem;border:1px solid #37485c;border-radius:8px}.availability-reference .row{align-items:start}.availability-status{font-weight:600;color:#efd599}.availability-actionable:empty{display:none}\n'
    (dest/'phase7.css').write_text(css,encoding='utf-8')
    for name in UI_FILES[:5]:
        text=(dest/name).read_text(encoding='utf-8')
        text=text.replace('<body ',f'<body data-availability-season="{overlay["season"]}" data-availability-week="{overlay["week"]}" data-availability-run="{overlay["prediction_run_id"]}" ',1)
        text=text.replace('<main id="main">','<main id="main"><section id="availability-notice" class="availability-notice" role="status" aria-live="polite">Current eligibility is loading. Original forecasts are preserved.</section>',1)
        text=text.replace('FRONTEND V3.1 · FROZEN FORECASTS','FRONTEND V3.1 · FROZEN FORECASTS + AVAILABILITY')
        marker='<script src="phase7.js'
        if marker not in text:raise ValueError('Candidate script entry missing')
        text=text.replace(marker,f'<script src="availability_overlay.js?v={sha256_file(dest/"availability_overlay.js")[:16]}"></script>'+marker,1)
        for asset in ['phase7.js','boards.js','phase7.css']:
            text=re.sub(re.escape(asset)+r'\?v=[0-9a-f]{16}',asset+'?v='+sha256_file(dest/asset)[:16],text)
        (dest/name).write_text(text,encoding='utf-8')
    files={str(p.relative_to(dest)).replace('\\','/'):sha256_file(p) for p in dest.rglob('*') if p.is_file() and p.name not in ['release_manifest.json','frontend_release.json']}
    content=digest_files(files);rid='frontend-v3-1-'+content[:16]
    identity={'release_id':rid,'prediction_run_id':base['prediction_run_id'],
        'ui_sha256':{n:files[n] for n in (*UI_FILES,'v31.js','receiver_discovery_v2.js','availability_overlay.js')},
        'data_sha256':{n:h for n,h in files.items() if n.startswith('data/')}}
    (dest/'data/frontend_release.json').write_text(json.dumps(identity,indent=2),encoding='utf-8')
    manifest={**base,'release_id':rid,'content_sha256':content,'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'base_release_id':base['release_id'],'availability_overlay_schema':SCHEMA,'availability_valid_until_utc':overlay['valid_until_utc'],
        'data_parity':audit_data(settings,dest),
        'files':{str(p.relative_to(dest)).replace('\\','/'):sha256_file(p) for p in dest.rglob('*') if p.is_file() and p.name!='release_manifest.json'}}
    (dest/'release_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    validate_release(dest,settings=settings)
    return {'path':str(dest),'release_id':rid,'manifest_sha256':sha256_file(dest/'release_manifest.json'),'published':False,'global_release_approval':'NOT_GRANTED'}
