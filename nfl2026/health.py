from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from nfl2026.config import load_settings
from nfl2026.deployment import validate_frontend_dist
from nfl2026.frontend.build import select_official_bundle


def _latest_metadata(root: Path) -> tuple[Path | None, dict]:
    choices: list[tuple[pd.Timestamp, Path, dict]] = []
    for path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            point = pd.Timestamp(meta.get("created_at_utc") or meta.get("retrieved_at_utc"))
            choices.append((point, path, meta))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if not choices:
        return None, {}
    _, path, meta = max(choices, key=lambda item: (item[0], str(item[1])))
    return path, meta


def collect_health(season: int | None = None, week: int | None = None) -> dict[str, object]:
    settings = load_settings()
    season = season or settings.current_season
    if week is None:
        roots = sorted((settings.prediction_root / str(season)).glob("week_*")) if (settings.prediction_root / str(season)).exists() else []
        week = int(roots[-1].name.split("_")[-1]) if roots else 1
    warnings: list[str] = []
    prediction_meta: dict = {}
    try:
        bundle = select_official_bundle(settings, season, week)
        prediction_meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
        games = len(pd.read_csv(bundle / "games.csv"))
        players = len(pd.read_csv(bundle / "players.csv"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        bundle = None
        games = players = 0
        warnings.append("No eligible official prediction bundle")
    market_path, market_meta = _latest_metadata(settings.root / "markets" / "snapshots" / str(season) / f"week_{week:02d}")
    value_path, value_meta = _latest_metadata(settings.root / "value_runs" / str(season) / f"week_{week:02d}")
    if not market_meta or market_meta.get("status") != "READY":
        warnings.append("Live market data unavailable")
    market_age = None
    if market_meta.get("retrieved_at_utc"):
        market_age = round((pd.Timestamp.now(tz="UTC") - pd.Timestamp(market_meta["retrieved_at_utc"])).total_seconds() / 60, 1)
    frontend_errors = validate_frontend_dist(settings.root / "frontend" / "dist")
    if frontend_errors:
        warnings.append("Frontend validation failed")
    deployment_path = settings.root / "deploy" / "status.json"
    deployment = json.loads(deployment_path.read_text(encoding="utf-8")) if deployment_path.exists() else {"status": "NOT_BUILT", "public_url": None}
    test_status_path = settings.report_root / "phase5_test_status.json"
    test_status = json.loads(test_status_path.read_text(encoding="utf-8")) if test_status_path.exists() else {"status": "NOT_RUN", "tests": None}
    if test_status.get("status") != "PASS":
        warnings.append("Phase 5 test status is not PASS")
    status = "BLOCKED" if bundle is None or frontend_errors else ("READY_WITH_WARNINGS" if warnings else "READY")
    return {
        "status": status,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "week": week,
        "latest_official_prediction": prediction_meta.get("run_id"),
        "prediction_stage": prediction_meta.get("weekly_stage"),
        "football_data_cutoff_utc": prediction_meta.get("cutoff_utc"),
        "games": games,
        "players": players,
        "latest_market_snapshot": market_meta.get("snapshot_id"),
        "market_status": market_meta.get("status", "MARKET_DATA_UNAVAILABLE"),
        "market_age_minutes": market_age,
        "latest_value_run": value_meta.get("value_run_id"),
        "value_status": value_meta.get("status", "MARKET_DATA_UNAVAILABLE"),
        "frontend_status": "PASS" if not frontend_errors else "FAIL",
        "frontend_errors": frontend_errors,
        "test_status": test_status,
        "deployment": deployment,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="NFL 2026 operational health")
    parser.add_argument("--season", type=int)
    parser.add_argument("--week", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    health = collect_health(args.season, args.week)
    if args.json:
        print(json.dumps(health, indent=2, default=str))
    else:
        print(f"NFL 2026 HEALTH - {health['status']}")
        print(f"Season {health['season']} | Week {health['week']}")
        print(f"Model: {health['latest_official_prediction'] or 'unavailable'} ({health['prediction_stage'] or 'n/a'})")
        print(f"Projected: {health['games']} games | {health['players']} players")
        print(f"Market: {health['market_status']} | age {health['market_age_minutes'] if health['market_age_minutes'] is not None else 'n/a'} min")
        print(f"Frontend: {health['frontend_status']} | Tests: {health['test_status'].get('status')}")
        print(f"Deployment: {health['deployment'].get('status')} | {health['deployment'].get('public_url') or 'no public URL'}")
        for warning in health["warnings"]:
            print(f"[WARN] {warning}")
    return 1 if health["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())

