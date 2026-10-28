from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid
from typing import Any

import pandas as pd

from nfl2026.config import Settings
from nfl2026.markets.core import validate_market_snapshot
from nfl2026.utilities.io import sha256_file


def _week_root(settings: Settings, season: int, week: int) -> Path:
    return settings.root / "markets" / "snapshots" / str(season) / f"week_{week:02d}"


def current_pointer_path(settings: Settings, season: int, week: int) -> Path:
    return _week_root(settings, season, week) / "current.json"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def snapshot_is_valid(snapshot: Path, season: int, week: int) -> bool:
    try:
        validate_candidate(snapshot, season, week)
        return True
    except Exception:
        return False


def resolve_last_known_good(settings: Settings, season: int, week: int) -> Path | None:
    week_root = _week_root(settings, season, week)
    pointer = current_pointer_path(settings, season, week)
    if pointer.is_file():
        try:
            value = json.loads(pointer.read_text(encoding="utf-8"))
            target = week_root / str(value["snapshot_id"])
            if snapshot_is_valid(target, season, week):
                return target
        except Exception:
            pass
    choices: list[tuple[str, Path]] = []
    if week_root.exists():
        for candidate in week_root.iterdir():
            if candidate.is_dir() and snapshot_is_valid(candidate, season, week):
                meta = json.loads((candidate / "metadata.json").read_text(encoding="utf-8"))
                if meta.get("lkg_status") not in {None, "CURRENT"}:
                    continue
                choices.append((str(meta.get("retrieved_at_utc", "")), candidate))
    return max(choices, default=("", None), key=lambda item: (item[0], item[1].name if item[1] else ""))[1]


def validate_candidate(snapshot: Path, season: int, week: int, expected_events: int | None = None) -> dict[str, Any]:
    meta = validate_market_snapshot(snapshot, season, week)
    if meta.get("status") != "READY":
        raise ValueError(f"Candidate provider status is not READY: {meta.get('status')}")
    quotes = pd.read_csv(snapshot / "market_quotes.csv")
    if quotes.empty or int(meta.get("quote_count", 0)) <= 0:
        raise ValueError("Candidate has zero market quotes")
    if quotes.quote_id.duplicated().any():
        raise ValueError("Candidate contains duplicate quotes")
    keys = ["provider_event_id", "bookmaker", "market_type", "selection", "side", "line"]
    if quotes.duplicated(keys).any():
        raise ValueError("Candidate contains conflicting duplicate quote selections")
    main = quotes[quotes.market_type.isin(["MONEYLINE", "SPREAD", "TOTAL"])]
    if main.empty:
        raise ValueError("Candidate has no supported game markets")
    raw = json.loads((snapshot / "raw_payload.json").read_text(encoding="utf-8"))
    events = raw.get("featured", []) if isinstance(raw, dict) else []
    if expected_events is not None and len(events) < expected_events:
        raise ValueError(f"Candidate only covers {len(events)}/{expected_events} expected events")
    # Every returned target event must contain all three requested main market types.
    for event in events:
        present = {
            {"h2h": "MONEYLINE", "spreads": "SPREAD", "totals": "TOTAL"}.get(str(market.get("key")))
            for book in event.get("bookmakers", [])
            for market in book.get("markets", [])
        }
        if not {"MONEYLINE", "SPREAD", "TOTAL"}.issubset(present):
            raise ValueError(f"Candidate event {event.get('id', '')} is missing a requested game market")
    if not events:
        raise ValueError("Candidate has no event payload")
    return meta


def promote_market_snapshot(settings: Settings, snapshot: Path, season: int, week: int, expected_events: int | None = None, promoted_at: datetime | None = None) -> dict[str, Any]:
    candidate_meta = validate_candidate(snapshot, season, week, expected_events)
    previous = resolve_last_known_good(settings, season, week)
    if previous is not None and previous.resolve() != snapshot.resolve():
        old = json.loads((previous / "raw_payload.json").read_text(encoding="utf-8"))
        new = json.loads((snapshot / "raw_payload.json").read_text(encoding="utf-8"))
        old_books = {b.get("key") for e in old.get("featured", []) for b in e.get("bookmakers", []) if b.get("key")}
        new_books = {b.get("key") for e in new.get("featured", []) for b in e.get("bookmakers", []) if b.get("key")}
        if old_books and not old_books.issubset(new_books):
            raise ValueError(f"Candidate lost previously available bookmaker(s): {', '.join(sorted(old_books - new_books))}")
    timestamp = (promoted_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    candidate_meta.update({
        "status": "READY",
        "lkg_status": "PROMOTION_PENDING",
        "promoted_at_utc": timestamp,
        "last_successful_refresh_utc": candidate_meta.get("retrieved_at_utc"),
        "source_snapshot_id": candidate_meta.get("snapshot_id"),
        "source_snapshot_hash": sha256_file(snapshot / "raw_payload.json"),
        "row_count": int(candidate_meta.get("quote_count", 0)),
    })
    quote_rows = pd.read_csv(snapshot / "market_quotes.csv")
    candidate_meta["event_count"] = int(quote_rows.provider_event_id.nunique())
    candidate_meta["bookmaker_count"] = int(quote_rows.bookmaker.nunique())
    meta_path = snapshot / "metadata.json"
    meta_path.write_text(json.dumps(candidate_meta, indent=2, sort_keys=True, default=str), encoding="utf-8")
    pointer = current_pointer_path(settings, season, week)
    atomic_json(pointer, {
        "snapshot_id": snapshot.name,
        "season": season,
        "week": week,
        "status": "CURRENT",
        "promoted_at_utc": timestamp,
        "last_successful_refresh_utc": candidate_meta["last_successful_refresh_utc"],
        "source_snapshot_hash": candidate_meta["source_snapshot_hash"],
    })
    candidate_meta["lkg_status"] = "CURRENT"
    meta_path.write_text(json.dumps(candidate_meta, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return candidate_meta


def record_refresh_attempt(settings: Settings, season: int, week: int, status: str, warnings: list[str] | tuple[str, ...], usage: dict[str, Any] | None = None, attempted_at: datetime | None = None) -> Path:
    attempted = (attempted_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = settings.root / "markets" / "refresh_attempts" / str(season) / f"week_{week:02d}"
    target = root / f"attempt-{attempted.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
    existing = resolve_last_known_good(settings, season, week)
    lkg_meta = json.loads((existing / "metadata.json").read_text(encoding="utf-8")) if existing else {}
    atomic_json(target, {
        "season": season, "week": week, "attempted_at_utc": attempted.isoformat(),
        "status": status, "warnings": list(warnings), "usage": usage or {},
        "last_known_good_snapshot_id": lkg_meta.get("snapshot_id"),
        "last_successful_refresh_utc": lkg_meta.get("last_successful_refresh_utc", lkg_meta.get("retrieved_at_utc")),
    })
    return target


def snapshot_age_minutes(snapshot: Path, now: datetime | None = None) -> float:
    meta = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    retrieved = pd.Timestamp(meta.get("last_successful_refresh_utc", meta["retrieved_at_utc"]))
    current = pd.Timestamp(now or datetime.now(timezone.utc))
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    return max(0.0, (current - retrieved.tz_convert("UTC")).total_seconds() / 60.0)


def freshness_state(snapshot: Path | None, max_age_minutes: int, now: datetime | None = None) -> str:
    if snapshot is None:
        return "UNAVAILABLE"
    return "CURRENT" if snapshot_age_minutes(snapshot, now) <= max_age_minutes else "STALE"
