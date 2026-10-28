from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from nfl2026.utilities.io import sha256_file

PROTECTED = ("predictions/2026", "locks/2026", "artifacts/official_freezes/2026",
    "artifacts/models", "artifacts/registry", "markets/snapshots", "value_runs",
    "data/snapshots/2026", "frontend/dist", "frontend/public", "deploy/artifacts",
    "deploy/github-pages", "deploy/status.json", "config/nfl2026.toml")


def json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def protect(root: Path) -> dict:
    files = []
    for relative in PROTECTED:
        path = root / relative
        files.extend([path] if path.is_file() else [p for p in path.rglob("*")
                      if p.is_file() and ".git" not in p.relative_to(path).parts])
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(set(files))}


def verify_protected(root: Path, baseline: dict) -> None:
    current = protect(root)
    if current != baseline:
        changed = [name for name in set(current) | set(baseline) if current.get(name) != baseline.get(name)]
        raise RuntimeError(f"Phase 7 protected production state changed: {changed[:10]}")


def verify_champions(settings) -> dict:
    from nfl2026.registry.core import load_registered_model
    expected = {"team_score_model": "phase2-v2", "player_td_model": "phase2-v2",
                "player_opportunity_model": "phase2-v3-rush-et"}
    if (settings.phase2_model_version, settings.phase2_player_model_version, settings.phase2_feature_version) != (
        "phase2-v2", "phase2-v3-rush-et", "advanced-pit-v2"):
        raise ValueError("Phase 7 cannot silently change production configuration")
    contracts = {}
    for name, version in expected.items():
        path, row = load_registered_model(settings.artifact_root / "registry/model_registry.csv", name, version,
                                         settings.phase2_feature_version)
        contracts[name] = {"version": version, "sha256": row["artifact_sha256"]}
    run = settings.prediction_root / "2026/week_06/phase3_5-production-20261014T130712Z-5aba9259"
    metadata = json.loads((run / "metadata.json").read_text())
    for name, digest in metadata["file_hashes"].items():
        if sha256_file(run / name) != digest:
            raise ValueError("Official Week 6 artifact hash mismatch")
    freeze = settings.artifact_root / "official_freezes/2026/week_06/freeze-20261014T130739Z-98675c85"
    if not (freeze / "official_predictions.csv").is_file():
        raise ValueError("Official Week 6 freeze missing")
    contracts["verified_at_utc"] = datetime.now(timezone.utc).isoformat()
    return contracts
