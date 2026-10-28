from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def schema_fingerprint(frame: pd.DataFrame) -> str:
    contract = "|".join(f"{column}:{frame[column].dtype}" for column in frame.columns)
    return sha256_text(contract)


def write_csv_json(frame: pd.DataFrame, csv_path: Path, json_path: Path | None = None) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(csv_path, index=False, lineterminator="\n")
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(frame.to_json(orient="records", indent=2), encoding="utf-8")


def write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def schema_fingerprint(frame: pd.DataFrame) -> str:
    contract = "|".join(f"{column}:{frame[column].dtype}" for column in frame.columns)
    return sha256_text(contract)
