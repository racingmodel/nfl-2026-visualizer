"""Bounded GitHub Pages propagation verification; never republishes on retry."""
from __future__ import annotations

import hashlib
import json
import time
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen


class PropagationFailure(RuntimeError):
    def __init__(self, record):
        self.record = record
        super().__init__(f"Public dashboard verification failed: {record['last_error']}")


def build_deployment_fingerprint(candidate, repository):
    """Bind immutable source bytes to exact committed Pages upload bytes.

    Git's CRLF -> LF clean conversion is the ONLY supported transformation.
    This is proved byte-for-byte against the actual Git blob, not performed on
    arbitrary HTTP responses. Binary assets and all other changes fail closed.
    No Git write, fetch, push or site mutation occurs here.
    """
    from nfl2026.frontend.v3_release import validate_release
    candidate, repository = Path(candidate).resolve(), Path(repository).resolve()
    manifest = validate_release(candidate, require_current=False)
    identity = json.loads((candidate / 'data/frontend_release.json').read_text(encoding='utf-8'))
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repository)
    if git('branch', '--show-current').decode().strip() != 'main':
        raise ValueError('Pages deployment must use the main branch')
    commit = git('rev-parse', 'HEAD').decode().strip()
    source_files = {**manifest['files'], 'release_manifest.json': hashlib.sha256((candidate/'release_manifest.json').read_bytes()).hexdigest()}
    tracked = set(git('ls-tree', '-r', '--name-only', commit, '--', 'site').decode().splitlines())
    if tracked != {'site/'+name for name in source_files} | {'site/.nojekyll'}:
        raise ValueError('Committed Pages destination differs from exact candidate file set')
    if git('show', commit+':site/.nojekyll') != b'':
        raise ValueError('Unexpected Pages .nojekyll content')
    deployed, transformations = {}, {}
    for name, digest in source_files.items():
        source = (candidate/name).read_bytes()
        blob = git('show', commit+':site/'+name)
        if hashlib.sha256(source).hexdigest() != digest:
            raise ValueError('Candidate changed during deployment audit')
        if blob != source:
            if (Path(name).suffix not in {'.html', '.js', '.css', '.json', '.svg', '.txt'}
                    or b'\x00' in source or b'\r\n' not in source
                    or blob != source.replace(b'\r\n', b'\n')):
                raise ValueError('Unsupported Git deployment transformation: '+name)
            source.decode('utf-8'); blob.decode('utf-8')
            transformations[name] = 'GIT_CLEAN_CRLF_TO_LF'
        deployed[name] = hashlib.sha256(blob).hexdigest()
    return {'schema': 'frontend-git-deployment-v1', 'release_id': identity['release_id'],
        'prediction_run_id': identity['prediction_run_id'], 'deployment_commit': commit,
        'branch': 'main', 'destination': 'site', 'build_timestamp': manifest['created_at_utc'],
        'source_manifest_sha256': source_files['release_manifest.json'],
        'source_sha256': source_files, 'asset_sha256': deployed, 'transformations': transformations}


def require_release_choice(deployment_status, release_selected):
    """Once V3 is deployed, a routine legacy build cannot silently downgrade it."""
    previous=deployment_status.get('previous_deployment') or {}
    active=deployment_status.get('frontend_release_id') or deployment_status.get('last_verified_frontend_release_id') or previous.get('frontend_release_id') or (deployment_status.get('smoke_test') or {}).get('expected_release_id')
    if active and not release_selected:
        raise ValueError("Frontend V3 is deployed; select a reviewed --release-candidate rather than the legacy build")


def publish_and_verify(repository, status_path: Path, metadata, *, changed, run_command,
                       public_url, verifier=None, progress=None, release_identity=None,
                       fingerprint_factory=None):
    """Integration seam: one push, read-only propagation retries, durable failures.

    Call only after the existing validated build/staged-diff checks. Tests inject
    Git and HTTP implementations; this helper is never invoked automatically.
    """
    verifier = verifier or verify_public_run
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    previous = {k: status.get(k) for k in ("status", "expected_run_id", "git_commit", "published_at_utc", "smoke_test", "frontend_release_id")}
    for key in ("git_commit", "published_at_utc", "smoke_test", "frontend_release_id", "deployment_fingerprint"):
        status.pop(key,None)
    status.update({"expected_run_id": metadata["run_id"], "previous_deployment": previous,
                   "public_url": public_url})
    def save():
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    count = 0
    try:
        if changed:
            message = f"NFL {metadata.get('season')} Week {metadata.get('week')} - {metadata['run_id']}"
            run_command(["git", "commit", "-m", message], repository)
            fingerprint = fingerprint_factory() if fingerprint_factory else None
            if fingerprint:
                status['deployment_fingerprint'] = fingerprint
                status['git_commit'] = fingerprint['deployment_commit']
                status['frontend_release_id'] = fingerprint['release_id']
                save()
            run_command(["git", "push", "origin", "main"], repository)
        else:
            # A failed previous push may leave a committed, unstaged release.
            # Identical files do not justify a duplicate commit, but pending
            # commits must still be delivered rather than falsely verified.
            pending = run_command(["git", "rev-list", "--count", "origin/main..HEAD"], repository)
            count = int(pending.stdout.strip())
            if count < 0:
                raise ValueError("Invalid pending commit count")
            fingerprint = fingerprint_factory() if fingerprint_factory else None
            if fingerprint:
                status['deployment_fingerprint'] = fingerprint
                status['git_commit'] = fingerprint['deployment_commit']
                status['frontend_release_id'] = fingerprint['release_id']
                save()
            if count:
                run_command(["git", "push", "origin", "main"], repository)
        status["status"] = "PUSHED" if changed or count else "PROPAGATING"
        save()
        def on_state(record):
            status.update({"status": record["status"], "smoke_test": record})
            save()
            if progress:
                progress(record)
        options={"on_state":on_state}
        if release_identity:
            options["expected_release"]=release_identity
        if fingerprint:
            options['deployment_fingerprint'] = fingerprint
        result = verifier(public_url, metadata["run_id"], **options)
        status.update({"status": "VERIFIED", "smoke_test": result,
            "git_commit": run_command(["git", "rev-parse", "HEAD"], repository).stdout.strip(),
            "published_at_utc": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
        if release_identity:
            status['frontend_release_id']=release_identity['release_id']
            status['last_verified_frontend_release_id']=release_identity['release_id']
        status.pop("last_error", None)
        save()
        return result
    except Exception as exc:
        status.update({"status": "FAILED", "last_error": f"{type(exc).__name__}: {exc}"})
        if isinstance(exc, PropagationFailure):
            status["smoke_test"] = exc.record
        save()
        raise


def verify_public_run(public_url, expected_run_id, *, attempts=10, max_seconds=240,
                      opener=urlopen, clock=time.monotonic, sleep=time.sleep, on_state=None, expected_release=None,
                      deployment_fingerprint=None):
    if attempts < 1 or max_seconds <= 0 or not expected_run_id:
        raise ValueError("Positive propagation bounds and exact expected run ID required")
    started = clock()
    nonce = hashlib.sha256(f"{expected_run_id}:{time.time_ns()}".encode()).hexdigest()[:12]
    url = public_url.rstrip("/") + "/data/current_week.json"
    record = {"status": "PROPAGATING", "url": url, "expected_run_id": expected_run_id,
              "attempts": 0, "last_error": "not inspected", "observed_run_id": None, "history": []}
    if expected_release:
        from pathlib import PurePosixPath
        if expected_release.get("prediction_run_id")!=expected_run_id or not expected_release.get("release_id") or not expected_release.get("ui_sha256"):
            raise ValueError("Release/run verification identity incomplete")
        for name,digest in {**expected_release["ui_sha256"],**expected_release.get('data_sha256',{})}.items():
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or any(c in name for c in ('\\','?', '#',':')) or not isinstance(digest,str) or not __import__('re').fullmatch('[0-9a-f]{64}',digest):
                raise ValueError("Unsafe release asset identity")
        record["expected_release_id"]=expected_release["release_id"]
    assets = {**expected_release['ui_sha256'], **expected_release.get('data_sha256', {})} if expected_release else {}
    if deployment_fingerprint:
        f = deployment_fingerprint
        if (not expected_release or f.get('schema') != 'frontend-git-deployment-v1'
                or f.get('release_id') != expected_release['release_id']
                or f.get('prediction_run_id') != expected_run_id
                or f.get('destination') != 'site' or f.get('branch') != 'main'
                or not __import__('re').fullmatch('[0-9a-f]{40,64}', f.get('deployment_commit', ''))
                or any(f.get('source_sha256', {}).get(k) != v for k,v in assets.items())
                or not set(assets).issubset(f.get('asset_sha256', {}))):
            raise ValueError('Deployment fingerprint incompatible with release')
        assets = f['asset_sha256']
        from pathlib import PurePosixPath
        for name,digest in assets.items():
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or any(c in name for c in ('\\','?', '#',':')) or not isinstance(digest,str) or not __import__('re').fullmatch('[0-9a-f]{64}',digest):
                raise ValueError('Unsafe deployment asset fingerprint')
            if digest != f['source_sha256'].get(name) and (f.get('transformations',{}).get(name) != 'GIT_CLEAN_CRLF_TO_LF' or Path(name).suffix not in {'.html','.js','.css','.json','.svg','.txt'}):
                raise ValueError('Unproven deployed asset transformation')
        record['deployment_commit'] = f['deployment_commit']
        record['deployment_transformations'] = f['transformations']
    record['http_observations'] = []
    def retrieve(path, attempt):
        remaining=max_seconds-(clock()-started)
        if remaining<=0:raise ValueError("propagation deadline exceeded")
        request=Request(public_url.rstrip('/')+'/'+path+f'?verification={nonce}-{attempt}',headers={"Cache-Control":"no-cache","Pragma":"no-cache"})
        with opener(request,timeout=min(15,remaining)) as response:
            if getattr(response,'status',200)!=200:raise ValueError("Public verification did not return HTTP 200")
            # Cap response size and check the same deadline between read chunks.
            content=bytearray()
            while True:
                chunk=response.read(65536)
                if not chunk:break
                content.extend(chunk)
                if len(content)>4*1024*1024:raise ValueError("Public response exceeds verification size limit")
                if clock()-started>max_seconds:raise ValueError("propagation deadline exceeded during retrieval")
        if clock()-started>max_seconds:raise ValueError("propagation deadline exceeded during retrieval")
        headers = getattr(response, 'headers', {})
        header_values = {name:headers.get(name) for name in ('ETag','Last-Modified','Cache-Control','Age','X-Cache','X-Cache-Hits','Content-Type')}
        content = bytes(content)
        record['http_observations'].append({'attempt':attempt+1, 'path':path,
            'sha256':hashlib.sha256(content).hexdigest(), 'headers':header_values})
        return content
    def emit(state):
        record["status"] = state
        record["elapsed_seconds"] = max(0, clock() - started)
        if on_state:
            on_state(dict(record))
    emit("PROPAGATING")
    for attempt in range(attempts):
        remaining = max_seconds - (clock() - started)
        if remaining <= 0:
            break
        record["attempts"] = attempt + 1
        try:
            payload = json.loads(retrieve('data/current_week.json',attempt).decode("utf-8"))
            if clock() - started > max_seconds:
                record["last_error"] = "propagation deadline exceeded during retrieval"
                break
            if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
                raise ValueError("Public bundle metadata schema invalid")
            observed = payload["metadata"].get("run_id")
            record["observed_run_id"] = observed
            if observed == expected_run_id:
                if expected_release:
                    live_release=json.loads(retrieve('data/frontend_release.json',attempt).decode('utf-8'))
                    if not isinstance(live_release,dict):
                        raise ValueError("Public frontend release metadata schema invalid")
                    record['observed_release_id']=live_release.get('release_id')
                    if live_release!=expected_release:
                        raise ValueError("Public frontend release identity differs from expected candidate")
                    for name,digest in assets.items():
                        observed_digest=hashlib.sha256(retrieve(name,attempt)).hexdigest()
                        if observed_digest!=digest:
                            record['mismatched_asset']={'path':name,'expected_sha256':digest,'observed_sha256':observed_digest}
                            raise ValueError(f"Public frontend asset is stale/mismatched: {name}; expected SHA256 {digest}, observed {observed_digest}")
                    record.pop('mismatched_asset',None)
                    record['release_id']=live_release['release_id']
                record["last_error"] = None
                record["run_id"] = observed
                record["history"].append({"attempt": attempt+1, "state": "VERIFIED", "run_id": observed})
                emit("VERIFIED")
                return record
            record["last_error"] = f"expected {expected_run_id}, received {observed}"
            record["history"].append({"attempt": attempt+1, "state": "STALE_OR_MISMATCHED", "run_id": observed})
        except (OSError, ValueError) as exc:
            record["last_error"] = f"{type(exc).__name__}: {exc}"
            record["history"].append({"attempt": attempt+1, "state": "RETRIEVAL_ERROR", "error": str(exc)})
        emit("PROPAGATING")
        if attempt+1 < attempts:
            wait = min(30, 5 * 1.6**attempt, max(0, max_seconds - (clock() - started)))
            if wait:
                sleep(wait)
    emit("FAILED")
    raise PropagationFailure(record)
