from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import math
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

import pandas as pd

from nfl2026.markets.providers.base import MarketFetchResult
from nfl2026.markets.schema import MARKET_QUOTE_COLUMNS


MARKET_KEYS = {
    "h2h": "MONEYLINE",
    "spreads": "SPREAD",
    "totals": "TOTAL",
    "team_totals": "TEAM_TOTAL",
    "player_anytime_td": "ANYTIME_TD",
    "player_rush_yds": "RUSHING_YARDS",
    "player_reception_yds": "RECEIVING_YARDS",
    "player_pass_yds": "PASSING_YARDS",
}


def _quote_id(parts: list[object]) -> str:
    return hashlib.sha256("|".join(str(value) for value in parts).encode("utf-8")).hexdigest()[:24]


def _event_rows(event: dict, retrieved_at_utc: datetime) -> list[dict]:
    rows: list[dict] = []
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            market_key = str(market.get("key", ""))
            market_type = MARKET_KEYS.get(market_key)
            if market_type is None:
                continue
            source_time = market.get("last_update") or book.get("last_update")
            for outcome in market.get("outcomes", []):
                name = str(outcome.get("name", "")).strip()
                description = str(outcome.get("description", "")).strip()
                if market_type == "MONEYLINE":
                    selection, side, player = name, "TEAM", ""
                elif market_type == "SPREAD":
                    selection, side, player = name, "TEAM", ""
                elif market_type == "TOTAL":
                    selection, side, player = name.upper(), name.upper(), ""
                elif market_type == 'TEAM_TOTAL':
                    selection, side, player = description, name.upper(), ''
                elif market_type == "ANYTIME_TD":
                    player = description or name
                    selection, side = player, name.upper() if name.upper() in {'YES','NO'} else 'YES'
                else:
                    player = description
                    selection, side = f"{player} {name}".strip(), name.upper()
                line = outcome.get("point")
                price = outcome.get("price")
                row = {
                    "provider": "the_odds_api",
                    "provider_event_id": str(event.get("id", "")),
                    "bookmaker": str(book.get("title") or book.get("key") or ""),
                    "market_type": market_type,
                    "selection": selection,
                    "selection_team": "",
                    "side": side,
                    "line": line,
                    "decimal_odds": price,
                    "home_team_name": str(event.get("home_team", "")),
                    "away_team_name": str(event.get("away_team", "")),
                    "kickoff_utc": event.get("commence_time"),
                    "player_name": player,
                    "player_team": "",
                    "player_position": "",
                    "player_id": "",
                    "source_timestamp_utc": source_time,
                    "retrieval_timestamp_utc": retrieved_at_utc.isoformat(),
                    "suspended": bool(outcome.get("suspended", False) or not outcome.get("active", True)),
                    # The provider response is a captured quote, not proof of
                    # jurisdiction-specific grading or executable liquidity.
                    "quote_type": "BACK", "venue": "BOOKMAKER", "executable": False,
                }
                row["quote_id"] = _quote_id([
                    row["provider"], row["provider_event_id"], row["bookmaker"],
                    market_type, selection, line, price, source_time,
                ])
                rows.append(row)
    return rows


def normalise_payload(payload: dict, retrieved_at_utc: datetime) -> pd.DataFrame:
    events: dict[str, dict] = {}
    featured = payload if isinstance(payload, list) else payload.get("featured", [])
    for event in featured or []:
        events[str(event.get("id", ""))] = event
    props = payload.get("props", {}) if isinstance(payload, dict) else {}
    if isinstance(props, list):
        props = {str(event.get("id", "")): event for event in props}
    for event_id, event in props.items():
        if not isinstance(event, dict):
            continue
        base = events.get(str(event_id), {})
        merged = {**base, **event, "id": event.get("id", event_id)}
        if base.get("bookmakers") and event.get("bookmakers"):
            merged["bookmakers"] = event["bookmakers"]
        events[f"props:{event_id}"] = merged
    rows: list[dict] = []
    for event in events.values():
        rows.extend(_event_rows(event, retrieved_at_utc))
    if not rows:
        return pd.DataFrame(columns=MARKET_QUOTE_COLUMNS)
    # Providers can repeat identical boards across components. Conflicting rows
    # sharing an ID remain visible for the candidate/QA rejection gate.
    return pd.DataFrame(rows).reindex(columns=MARKET_QUOTE_COLUMNS).drop_duplicates().reset_index(drop=True)


class TheOddsApiProvider:
    GAME_MARKETS = "h2h,spreads,totals"
    PROP_MARKETS = "player_anytime_td,player_rush_yds,player_reception_yds"
    name = "the_odds_api"
    @staticmethod
    def estimate_cost(markets: str, bookmaker_count: int, region_count: int = 1) -> int:
        market_count = len({item.strip() for item in markets.split(",") if item.strip()})
        groups = math.ceil(bookmaker_count / 10) if bookmaker_count else max(1, region_count)
        return market_count * groups

    def __init__(self, api_key: str | None = None, regions: str = "uk,us", sport_key: str = "americanfootball_nfl", timeout: int = 20, bookmakers: tuple[str, ...] | list[str] = (), quota_reserve: int = 50, low_credit_mode: bool = False, prop_window_hours: int = 48):
        self.api_key = api_key or os.getenv("ODDS_API_KEY")
        self.regions = regions
        self.sport_key = sport_key
        self.timeout = timeout
        self.bookmakers = tuple(dict.fromkeys(str(book) for book in bookmakers if str(book)))
        self.quota_reserve = max(0, int(quota_reserve))
        self.low_credit_mode = bool(low_credit_mode)
        self.prop_window_hours = max(0, int(prop_window_hours))
        self.usage_events: list[dict[str, object]] = []

    def _get(self, path: str, params: dict[str, str]) -> object:
        query = urlencode({**params, "apiKey": str(self.api_key)})
        last_error: Exception | None = None
        chargeable = bool(params.get("markets"))
        attempts = 1 if chargeable else 2
        for attempt in range(attempts):
            try:
                gate = getattr(self, "_retry_gate", None)
                if chargeable and gate is not None:
                    gate()
                with urlopen(f"https://api.the-odds-api.com/v4/{path}?{query}", timeout=self.timeout) as response:
                    headers = response.headers
                    self.usage_events.append({
                        "request_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "resource": path,
                        "markets_requested": params.get("markets", ""),
                        "bookmakers_requested": params.get("bookmakers", ""),
                        "regions_requested": params.get("regions", ""),
                        "x-requests-remaining": headers.get("x-requests-remaining"),
                        "x-requests-used": headers.get("x-requests-used"),
                        "x-requests-last": headers.get("x-requests-last"),
                        "quota_remaining": headers.get("x-requests-remaining"),
                        "quota_used": headers.get("x-requests-used"),
                        "request_cost": headers.get("x-requests-last"),
                    })
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                if isinstance(exc, HTTPError):
                    headers = exc.headers or {}
                    self.usage_events.append({
                        "request_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "resource": path,
                        "markets_requested": params.get("markets", ""),
                        "bookmakers_requested": params.get("bookmakers", ""),
                        "regions_requested": params.get("regions", ""),
                        "x-requests-remaining": headers.get("x-requests-remaining"),
                        "x-requests-used": headers.get("x-requests-used"),
                        "x-requests-last": headers.get("x-requests-last"),
                    })
                    if exc.code in {401, 403, 429}:
                        raise RuntimeError(f"The Odds API rejected the request (HTTP {exc.code})") from exc
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.25)
        raise RuntimeError(f"The Odds API retrieval failed safely after {attempts} attempt(s) ({type(last_error).__name__})")

    def fetch(self, season, week, retrieved_at_utc, target_fixtures=None, **kwargs):
        from nfl2026.markets.providers.scoped_fetch import fetch_scoped
        return fetch_scoped(self, season, week, retrieved_at_utc, target_fixtures, **kwargs)
