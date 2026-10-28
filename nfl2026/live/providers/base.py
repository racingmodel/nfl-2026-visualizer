from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass
class ProviderResult:
    name: str
    records: pd.DataFrame
    coverage: pd.DataFrame
    status: str
    source_url: str
    retrieved_at_utc: str
    source_known_at_utc: str
    source_known_at_basis: str
    reliability: str
    warnings: list[str] = field(default_factory=list)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def parse_nflverse_timestamp(path: Path) -> datetime:
    raw = json.loads(path.read_text(encoding="utf-8"))["last_updated"]
    # nflverse release timestamps are currently labelled EDT/EST rather than ISO.
    clean = raw.rsplit(" ", 1)[0]
    local = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc)


def fetch_bytes(url: str, attempts: int = 3, timeout_seconds: int = 30, opener: Callable | None = None) -> bytes:
    opener = opener or urlopen
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "NFL2026-live-provider/1.0"})
            with opener(request, timeout=timeout_seconds) as response:
                return response.read()
        except Exception as exc:  # network errors are recorded by the caller
            error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"Provider retrieval failed after {attempts} attempts: {url}: {error}")
