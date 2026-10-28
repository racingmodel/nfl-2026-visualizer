"""Immutable compact receiving presentation built from reviewed production bytes."""
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil

from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file


def build_compact_release(settings,output,name='receiving_release_candidate',source_release=None):
    from nfl2026.frontend.v3_release import UI_FILES,digest_files,validate_release,audit_data
    root=settings.root;destination=Path(output).resolve()/name
    if not destination.is_relative_to(root/'reports'):raise ValueError('Owned report directory required')
    if destination.exists():raise FileExistsError('Candidate is immutable')
    source=Path(source_release).resolve() if source_release is not None else root/'frontend/releases/frontend-v3-1-30be9141bd174f70'
    if not source.is_relative_to(root):raise ValueError('Validated project release required')
    validate_release(source,settings=settings)
    if 'function uniqueReceivingV3' in (source/'receiver_discovery_v2.js').read_text(encoding='utf-8'):raise ValueError('Compact layout already present; cannot append twice')
    shutil.copytree(source,destination)
    module='receiver_discovery_v2.js'
    # Bundle the new layout into the versioned receiver asset. Previously
    # published/source bytes remain intact; the new asset hash binds both parts.
    text=(destination/module).read_text(encoding='utf-8')+'\n'+(root/'frontend/phase7_src/receiving_list_v3.js').read_text(encoding='utf-8')
    (destination/module).write_text(text,encoding='utf-8')
    html=(destination/'index.html').read_text(encoding='utf-8')
    import re
    html=re.sub(r'receiver_discovery_v2.js\?v=[0-9a-f]{16}',f'{module}?v={sha256_file(destination/module)[:16]}',html)
    (destination/'index.html').write_text(html,encoding='utf-8')
    files={str(p.relative_to(destination)).replace('\\','/'):sha256_file(p) for p in destination.rglob('*') if p.is_file() and p.name not in ['release_manifest.json','frontend_release.json']}
    content=digest_files(files);rid='frontend-v3-1-'+content[:16]
    original=json.loads((source/'release_manifest.json').read_text())
    identity={'release_id':rid,'prediction_run_id':original['prediction_run_id'],
        'ui_sha256':{name:files[name] for name in (*UI_FILES,'v31.js',module)},
        'data_sha256':{name:digest for name,digest in files.items() if name.startswith('data/')}}
    json_write(destination/'data/frontend_release.json',identity)
    parity=audit_data(settings,destination)
    manifest={**original,'release_id':rid,'content_sha256':content,'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'files':{str(p.relative_to(destination)).replace('\\','/'):sha256_file(p) for p in destination.rglob('*') if p.is_file() and p.name!='release_manifest.json'},
        'data_parity':parity,'receiving_layout_schema':'compact-receiving-v3','base_release_id':original['release_id']}
    json_write(destination/'release_manifest.json',manifest);validate_release(destination,settings=settings)
    json_write(Path(output)/'receiving_candidate.json',{'path':str(destination.relative_to(root)),'release_id':rid,
        'manifest_sha256':sha256_file(destination/'release_manifest.json'),'source_prediction_run_id':original['prediction_run_id'],'data_parity':'PASS','published':False})
    return destination
