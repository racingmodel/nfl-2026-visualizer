from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from nfl2026.utilities.io import sha256_file, write_csv_json


def register_model(index_path: Path, artifact_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    required = {"model_name", "model_version", "feature_version", "target", "algorithm", "feature_list", "validation_metrics", "data_manifest_sha256"}
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"Missing registry metadata: {missing}")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_path.exists():
        artifact_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    row = {
        **metadata,
        "training_timestamp_utc": metadata.get("training_timestamp_utc", datetime.now(timezone.utc).isoformat()),
        "artifact_path": str(artifact_path),
        "artifact_sha256": sha256_file(artifact_path),
        "feature_list": json.dumps(metadata["feature_list"], sort_keys=True),
        "validation_metrics": json.dumps(metadata["validation_metrics"], sort_keys=True),
    }
    existing = pd.read_csv(index_path) if index_path.exists() else pd.DataFrame()
    if not existing.empty:
        duplicate = existing[
            (existing["model_name"] == row["model_name"]) & (existing["model_version"] == row["model_version"])
        ]
        if not duplicate.empty:
            prior = duplicate.iloc[-1]
            if prior["artifact_sha256"] != row["artifact_sha256"]:
                raise FileExistsError("Model version is immutable and already points to different content")
            return prior.to_dict()
    registry = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    write_csv_json(registry, index_path, index_path.with_suffix(".json"))
    return row


def load_registered_model(index_path: Path, model_name: str, model_version: str, feature_version: str) -> tuple[Path, dict[str, Any]]:
    if not index_path.exists():
        raise FileNotFoundError(f"Model registry not found: {index_path}")
    registry = pd.read_csv(index_path)
    match = registry[(registry["model_name"] == model_name) & (registry["model_version"] == model_version)]
    if match.empty:
        raise FileNotFoundError(f"No registered artifact for {model_name}:{model_version}")
    row = match.iloc[-1].to_dict()
    if row["feature_version"] != feature_version:
        raise ValueError(f"Feature version mismatch: requested {feature_version}, registered {row['feature_version']}")
    artifact = Path(row["artifact_path"])
    if not artifact.exists() or sha256_file(artifact) != row["artifact_sha256"]:
        raise ValueError("Registered artifact is missing or its checksum changed")
    return artifact, row


def append_run(index_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    required = {"run_id", "timestamp_utc", "season", "week", "data_cutoff_utc", "data_manifest_sha256", "feature_version", "code_version", "prediction_output_locations"}
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"Missing run metadata: {missing}")
    existing = pd.read_csv(index_path) if index_path.exists() else pd.DataFrame()
    if not existing.empty and metadata["run_id"] in set(existing["run_id"]):
        raise FileExistsError(f"Run ID is immutable: {metadata['run_id']}")
    row = dict(metadata)
    row["prediction_output_locations"] = json.dumps(row["prediction_output_locations"], sort_keys=True)
    registry = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    write_csv_json(registry, index_path, index_path.with_suffix(".json"))
    return row
