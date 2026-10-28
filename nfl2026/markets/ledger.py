from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from nfl2026.config import Settings
from nfl2026.markets.qualification import select_qualifying_values
from nfl2026.markets.schema import LEDGER_COLUMNS


def ledger_path(settings: Settings) -> Path:
    return settings.root / "value_runs" / "ledger" / "value_ledger.csv"


def ensure_ledger(settings: Settings) -> Path:
    path = ledger_path(settings)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=LEDGER_COLUMNS).to_csv(path, index=False, lineterminator="\n")
    else:
        existing = pd.read_csv(path)
        if list(existing.columns) != LEDGER_COLUMNS:
            raise ValueError('Existing ledger schema differs; explicit non-destructive migration required')
    return path


def append_qualifying_values(settings: Settings, value_run: Path) -> pd.DataFrame:
    # Exclusive local writer lock prevents two repeated executions reading the
    # same old IDs and appending duplicates. An orphan lock requires review;
    # it is never automatically stolen from an unknown writer.
    lock = ledger_path(settings).with_suffix('.append.lock')
    lock.parent.mkdir(parents=True,exist_ok=True)
    try:
        handle = lock.open('x',encoding='utf-8')
    except FileExistsError as exc:
        raise RuntimeError('Value ledger already has an active/unresolved writer') from exc
    try:
        with handle:
            handle.write(str(value_run));handle.flush()
            return _append_qualifying_values_locked(settings,value_run)
    finally:
        lock.unlink()


def _append_qualifying_values_locked(settings: Settings, value_run: Path) -> pd.DataFrame:
    metadata = json.loads((value_run / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("status") != "OFFICIAL_VALUE_SNAPSHOT" or metadata.get("mode") != "official":
        raise ValueError("Only a QA-passed official value snapshot may enter the ledger")
    if hashlib.sha256((value_run/'value_bets.csv').read_bytes()).hexdigest() != metadata.get('file_hashes',{}).get('value_bets.csv'):
        raise ValueError('Immutable value-run CSV hash mismatch')
    values = pd.read_csv(value_run / "value_bets.csv")
    selected = select_qualifying_values(values)
    path = ensure_ledger(settings)
    existing = pd.read_csv(path)
    rows = []
    for row in selected.to_dict("records"):
        entry_id = hashlib.sha256(f"{row['value_run_id']}|{row['quote_id']}".encode()).hexdigest()[:24]
        if len(existing) and entry_id in set(existing.ledger_entry_id.astype(str)):
            continue
        rows.append({
            "ledger_entry_id": entry_id, "captured_at_utc": metadata["created_at_utc"],
            "value_run_id": row["value_run_id"], "prediction_run_id": row["prediction_run_id"],
            "prediction_lock_id": row["prediction_lock_id"], "market_snapshot_id": row["market_snapshot_id"],
            "quote_id": row["quote_id"], "game_id": row["game_id"], "market_type": row["market_type"],
            "selection": row["selection"], "player_id": row.get("player_id", ""), "team": row.get("team", ""),
            "side": row.get("side", ""), "line": row.get("line"),
            "bookmaker": row["bookmaker"], "book_odds": row["book_odds"], "market_timestamp_utc": row["market_timestamp_utc"],
            "model_probability": row["model_probability"], "model_fair_odds": row["model_fair_odds"],
            "no_vig_probability": row.get("no_vig_probability"), "edge_pp": row["edge_pp"], "ev_pct": row["ev_pct"],
            "value_confidence": row["value_confidence"], "value_class": row["value_class"],
            "hypothetical_stake": 1.0, "settlement_status": "OPEN", "settlement_result": "", "hypothetical_return": "",
        })
    if rows:
        # Preserve original bytes. New rows append; schema drift never rewrites history.
        if path.stat().st_size and not path.read_bytes().endswith(b'\n'):
            raise ValueError('Ledger lacks terminal newline; explicit migration required')
        pd.DataFrame(rows).reindex(columns=LEDGER_COLUMNS).to_csv(path, mode='a',header=False,index=False,lineterminator='\n')
        updated = pd.read_csv(path)
        return updated
    return existing.reindex(columns=LEDGER_COLUMNS)
