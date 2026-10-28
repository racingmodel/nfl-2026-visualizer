from __future__ import annotations

import pandas as pd

from nfl2026.config import Settings


HISTORICAL_BENCHMARKS = {
    "score_mae": 7.458,
    "margin_mae": 10.345,
    "total_mae": 10.355,
    "rush_yards_mae": 7.873,
    "receiving_yards_mae": 15.633,
    "td_brier": 0.1496,
}


def drift_report(settings: Settings) -> pd.DataFrame:
    sources = {
        "score_mae": ("live_2026_team_performance.csv", "score_mae"),
        "margin_mae": ("live_2026_team_performance.csv", "margin_mae"),
        "total_mae": ("live_2026_team_performance.csv", "total_mae"),
        "rush_yards_mae": ("live_2026_player_performance.csv", "rush_yards_mae"),
        "receiving_yards_mae": ("live_2026_player_performance.csv", "receiving_yards_mae"),
        "td_brier": ("live_2026_td_calibration.csv", "td_brier"),
    }
    rows = []
    for metric, (filename, column) in sources.items():
        path = settings.report_root / filename
        frame = pd.read_csv(path) if path.exists() else pd.DataFrame()
        weeks = int(frame.week.nunique()) if len(frame) and "week" in frame else 0
        live = float(pd.to_numeric(frame[column], errors="coerce").mean()) if len(frame) and column in frame else float("nan")
        benchmark = HISTORICAL_BENCHMARKS[metric]
        status = "NOT_ENOUGH_DATA" if weeks < settings.drift_minimum_completed_weeks else ("DRIFT_WARNING" if live > benchmark * settings.drift_multiplier else "IN_RANGE")
        rows.append({"metric": metric, "historical_benchmark": benchmark, "live_value": live, "completed_weeks": weeks, "warning_threshold": benchmark * settings.drift_multiplier, "status": status})
    return pd.DataFrame(rows)
