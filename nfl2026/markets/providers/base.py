from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class MarketFetchResult:
    provider: str
    retrieved_at_utc: datetime
    quotes: pd.DataFrame
    raw_payload: dict
    status: str
    warnings: tuple[str, ...] = ()
    usage: dict[str, object] = field(default_factory=dict)


class MarketProvider(Protocol):
    name: str

    def fetch(self, season: int, week: int, retrieved_at_utc: datetime, target_fixtures: pd.DataFrame | None = None) -> MarketFetchResult:
        ...
