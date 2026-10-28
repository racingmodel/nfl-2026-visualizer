from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

from nfl2026.config import Settings
from nfl2026.utilities.io import sha256_file


REQUIRED_FRONTEND_FILES = (
    "index.html", "players.html", "value.html", "performance.html", "model.html",
    "styles.css", "app.js", "data/current_week.json", "data/value.json", "data/performance.json",
)


def validate_frontend_dist(dist: Path) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_FRONTEND_FILES:
        path = dist / relative
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing or empty: {relative}")
    for relative in ("data/current_week.json", "data/value.json", "data/performance.json"):
        path = dist / relative
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid JSON: {relative} ({type(exc).__name__})")
    secret_tokens = ("ODDS_API_KEY", "apiKey=", "api_key=")
    for path in dist.rglob("*") if dist.exists() else []:
        if path.is_file() and path.suffix.lower() in {".html", ".js", ".css", ".json"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(token in text for token in secret_tokens):
                errors.append(f"credential-like token found: {path.relative_to(dist)}")
            if re.search(r"(?i)\b[a-z]:(?:\\+|/+)users(?:\\+|/+)", text) or "file:///" in text.lower():
                errors.append(f"local filesystem path found: {path.relative_to(dist)}")
    return errors


def create_deployment_artifact(settings: Settings, created_at: datetime | None = None) -> Path:
    created_at = created_at or datetime.now(timezone.utc)
    dist = settings.root / "frontend" / "dist"
    errors = validate_frontend_dist(dist)
    if errors:
        raise RuntimeError("Deployment validation failed: " + "; ".join(errors))
    from nfl2026.production.partial_gate import require_certified_partial_frontend
    require_certified_partial_frontend(settings,dist)
    digest = hashlib.sha256("|".join(sha256_file(dist / name) for name in REQUIRED_FRONTEND_FILES).encode()).hexdigest()[:10]
    deployment_id = f"frontend-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{digest}"
    destination = settings.root / "deploy" / "artifacts" / deployment_id
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = settings.root / "deploy" / f".staging-{deployment_id}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(dist, staging)
    staged_errors = validate_frontend_dist(staging)
    if staged_errors:
        shutil.rmtree(staging)
        raise RuntimeError("Staged deployment validation failed: " + "; ".join(staged_errors))
    manifest = {
        "deployment_id": deployment_id,
        "created_at_utc": created_at.isoformat(),
        "status": "VALIDATED_NOT_PUBLISHED",
        "public_url": None,
        "files": {str(path.relative_to(staging)).replace("\\", "/"): sha256_file(path) for path in staging.rglob("*") if path.is_file()},
    }
    (staging / "deployment_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    staging.rename(destination)
    status_path = settings.root / "deploy" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return destination

