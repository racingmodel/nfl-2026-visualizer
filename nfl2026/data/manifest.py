from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

import pandas as pd

from nfl2026.config import Settings
from nfl2026.utilities.io import schema_fingerprint, sha256_file, write_csv_json


SOURCE_PATTERN = re.compile(
    r"^(?P<kind>schedules|weekly|rosters|depth_charts|injuries|seasonal|pbp)_(?P<season>20\d{2})(?:_(?P=season))?\.(?P<ext>csv|parquet)$"
)


def _read_source(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path, low_memory=False)


def discover_sources(settings: Settings) -> pd.DataFrame:
    candidates: list[dict[str, object]] = []
    for season in settings.training_seasons:
        folder = settings.legacy_root / f"{season} Stats"
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            match = SOURCE_PATTERN.match(path.name)
            if not match:
                continue
            canonical_name = f"{match['kind']}_{season}.{match['ext']}"
            canonical_path = folder / canonical_name
            candidates.append(
                {
                    "path": path,
                    "season": season,
                    "data_type": match["kind"],
                    "canonical_path": canonical_path,
                    "is_alias": path.name != canonical_name,
                }
            )

    rows: list[dict[str, object]] = []
    for item in candidates:
        path = item["path"]
        assert isinstance(path, Path)
        frame = _read_source(path)
        week = pd.to_numeric(frame.get("week", pd.Series(dtype=float)), errors="coerce")
        date_column = next((c for c in ("gameday", "game_date", "date_modified", "dt") if c in frame), None)
        parsed_dates = pd.to_datetime(frame[date_column], errors="coerce", utc=True) if date_column else pd.Series(dtype="datetime64[ns, UTC]")
        canonical_path = item["canonical_path"]
        assert isinstance(canonical_path, Path)
        digest = sha256_file(path)
        canonical_digest = sha256_file(canonical_path) if canonical_path.exists() else None
        rows.append(
            {
                "source": "nflverse_local_snapshot",
                "season": item["season"],
                "data_type": item["data_type"],
                "file_path": str(path.relative_to(settings.root)),
                "row_count": len(frame),
                "column_count": len(frame.columns),
                "schema_fingerprint": schema_fingerprint(frame),
                "minimum_date": parsed_dates.min().isoformat() if parsed_dates.notna().any() else "",
                "maximum_date": parsed_dates.max().isoformat() if parsed_dates.notna().any() else "",
                "minimum_week": int(week.min()) if week.notna().any() else "",
                "maximum_week": int(week.max()) if week.notna().any() else "",
                "duplicate_status": "exact_alias" if item["is_alias"] and digest == canonical_digest else ("variant_alias" if item["is_alias"] else "canonical"),
                "canonical_path": str(canonical_path.relative_to(settings.root)) if canonical_path.exists() else "",
                "last_modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "sha256": digest,
            }
        )
    return pd.DataFrame(rows).sort_values(["season", "data_type", "file_path"]).reset_index(drop=True)


def build_manifest(settings: Settings) -> pd.DataFrame:
    manifest = discover_sources(settings)
    write_csv_json(
        manifest,
        settings.artifact_root / "data_manifest.csv",
        settings.artifact_root / "data_manifest.json",
    )
    return manifest


def canonical_source(manifest: pd.DataFrame, season: int, data_type: str, settings: Settings) -> Path | None:
    rows = manifest[
        (manifest["season"] == season)
        & (manifest["data_type"] == data_type)
        & (manifest["duplicate_status"] == "canonical")
    ]
    if rows.empty:
        return None
    return settings.root / str(rows.iloc[0]["file_path"])
