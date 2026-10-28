"""Register reviewed immutable frontend bytes without rebuilding any forecast."""
import json
from pathlib import Path
import shutil

from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file


def verify_sealed_candidate(root, review, expected_release, expected_manifest):
    review=Path(review).resolve();root=Path(root).resolve()
    if not review.is_relative_to(root/'reports'):raise ValueError('Review must be inside project reports')
    seal=json.loads((review/'completed_manifest.json').read_text(encoding='utf-8'))
    if seal.get('schema')!='sealed-targeted-certification-v1':raise ValueError('Unsupported sealed review')
    relative=seal.get('authoritative_ui_candidate')
    if relative!='review_v2/ui_candidate':raise ValueError('Only the authoritative review_v2 artifact may be adopted')
    source=(review/relative).resolve()
    if not source.is_relative_to(review):raise ValueError('Candidate escapes sealed review')
    verified=0
    for base,entries in [(review,seal['file_sha256']),(root,seal['code_sha256'])]:
        for name,digest in entries.items():
            path=(base/name).resolve()
            if not path.is_relative_to(base) or sha256_file(path)!=digest:raise ValueError('Sealed review changed: '+name)
            verified+=1
    if sha256_file(source/'release_manifest.json')!=expected_manifest:raise ValueError('Expected manifest hash differs')
    manifest=json.loads((source/'release_manifest.json').read_text(encoding='utf-8'))
    if manifest['release_id']!=expected_release:raise ValueError('Expected release ID differs')
    from nfl2026.frontend.v3_release import validate_release
    validate_release(source)
    return source,manifest,verified


def adopt_reviewed_release(settings,review,expected_release,expected_manifest,output):
    """Only the candidate selector changes. An old PASS cannot approve new bytes."""
    output=Path(output).resolve()
    if not output.is_relative_to(settings.root/'reports') or not output.is_dir():raise ValueError('Existing report output required')
    source,manifest,verified=verify_sealed_candidate(settings.root,review,expected_release,expected_manifest)
    destination=settings.root/'frontend/releases'/expected_release
    if destination.name!=expected_release or Path(expected_release).name!=expected_release:raise ValueError('Unsafe release ID')
    if destination.exists():
        actual={str(p.relative_to(destination)):sha256_file(p) for p in destination.rglob('*') if p.is_file()}
        original={str(p.relative_to(source)):sha256_file(p) for p in source.rglob('*') if p.is_file()}
        if actual!=original:raise FileExistsError('Registered release differs; immutable bytes cannot be overwritten')
    else:shutil.copytree(source,destination)
    from nfl2026.frontend.v3_release import validate_release
    validate_release(destination,settings=settings)
    if sha256_file(destination/'release_manifest.json')!=expected_manifest:raise ValueError('Copied manifest differs')
    selected={'path':str(destination.relative_to(settings.root)),'release_id':expected_release,
        'manifest_sha256':expected_manifest,'source_prediction_run_id':manifest['prediction_run_id'],'data_parity':'PASS'}
    previous=settings.report_root/'phase75_release_candidate.json'
    backup=output/'prior_candidate_selector.json'
    if previous.exists() and not backup.exists():shutil.copyfile(previous,backup)
    json_write(previous,selected)
    record={**selected,'sealed_source':str(source.relative_to(settings.root)),
        'sealed_review_entries_verified':verified,'manifest_byte_identical':True,
        'official_forecasts_regenerated':False,'global_review_required':True,'published':False}
    json_write(output/'adoption.json',record)
    return destination
