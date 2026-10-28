from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.utilities.io import sha256_file, write_csv_json


SKILL_POSITIONS = {"QB", "RB", "FB", "WR", "TE"}


def external_root(settings: Settings) -> Path:
    return settings.root / "data" / "phase2_external"


def _read(path: Path, **kwargs) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False, **kwargs)


def build_phase2_source_assessment(settings: Settings) -> pd.DataFrame:
    root = external_root(settings)
    rows: list[dict[str, object]] = []
    specifications = [
        ("weekly_roster", range(2022, 2027), "WEEKLY_ASSUMED_PREGAME", True, "weekly roster snapshot; current status may change"),
        ("snap_counts", range(2022, 2026), "EXACT_POSTGAME_LAGGED", True, "game-level PFR snaps; used only after prior game"),
        ("participation", range(2022, 2026), "EXACT_POSTGAME_LAGGED", True, "play participation; route flag retained only where observed"),
        ("injuries", range(2025, 2027), "WEEKLY_ASSUMED_PREGAME", True, "2026 coverage is currently incomplete"),
    ]
    for data_type, seasons, reliability, allowed, note in specifications:
        for season in seasons:
            path = root / f"{data_type}_{season}.csv"
            rows.append({
                "data_type": data_type, "season": season, "path": str(path.relative_to(settings.root)),
                "exists": path.exists(), "rows": sum(1 for _ in path.open("rb")) - 1 if path.exists() else 0,
                "reliability": reliability if path.exists() else "NOT_AVAILABLE", "pregame_feature_allowed": bool(path.exists() and allowed),
                "sha256": sha256_file(path) if path.exists() else "", "note": note,
            })
    for data_type, path, reliability, allowed, note in [
        ("current_schedule", root / "games.csv", "EXACT_POINT_IN_TIME", True, "official current schedule; projected QB fields are 2026-only inputs"),
        ("legacy_global_roster", settings.legacy_root / "2025 Stats" / "rosters_2025.csv", "NOT_SAFE", False, "global latest-team table is not historical point-in-time"),
        ("legacy_depth_2022_2024", settings.legacy_root / "2024 Stats" / "depth_charts_2024.csv", "WEEKLY_ASSUMED_PREGAME", True, "weekly rank with no publication timestamp"),
        ("legacy_depth_2025", settings.legacy_root / "2025 Stats" / "depth_charts_2025.csv", "EXACT_POINT_IN_TIME", True, "timestamped snapshot; select strictly before kickoff"),
    ]:
        rows.append({
            "data_type": data_type, "season": "all" if "legacy" in data_type else 2026, "path": str(path.relative_to(settings.root)),
            "exists": path.exists(), "rows": sum(1 for _ in path.open("rb")) - 1 if path.exists() and path.suffix == ".csv" else 0,
            "reliability": reliability, "pregame_feature_allowed": allowed, "sha256": sha256_file(path) if path.exists() else "", "note": note,
        })
    out = pd.DataFrame(rows)
    write_csv_json(out, settings.report_root / "phase2_data_source_assessment.csv")
    return out


def load_weekly_rosters(settings: Settings, seasons: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)) -> pd.DataFrame:
    frames = []
    for season in seasons:
        path = external_root(settings) / f"weekly_roster_{season}.csv"
        if path.exists():
            frame = _read(path)
            frame["season"] = season
            frames.append(frame)
    roster = pd.concat(frames, ignore_index=True)
    roster["player_id"] = roster["gsis_id"].astype("string")
    roster["player_name"] = roster["full_name"].fillna(roster["football_name"])
    roster["position"] = roster["position"].fillna(roster["depth_chart_position"]).astype("string")
    roster["roster_reliability"] = "WEEKLY_ASSUMED_PREGAME"
    return roster


def build_pregame_universe(settings: Settings, rosters: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    fixtures = pd.concat([
        games[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team"]].rename(columns={"home_team": "team", "away_team": "opponent"}),
        games[["game_id", "season", "week", "kickoff_utc", "away_team", "home_team"]].rename(columns={"away_team": "team", "home_team": "opponent"}),
    ], ignore_index=True)
    eligible = rosters[
        rosters["game_type"].eq("REG") & rosters["position"].isin(SKILL_POSITIONS) &
        rosters["player_id"].notna() & rosters["status"].isin(["ACT", "INA"])
    ].copy()
    eligible["status_eligible"] = eligible["status"].eq("ACT")
    universe = eligible.merge(fixtures, on=["season", "week", "team"], how="inner")
    universe = universe.sort_values(["game_id", "team", "position", "player_id"]).drop_duplicates(["game_id", "player_id"])
    universe["universe_rule"] = "official_weekly_roster_skill_position_ACT"
    universe["universe_reliability"] = "WEEKLY_ASSUMED_PREGAME"
    universe["prediction_cutoff_utc"] = pd.to_datetime(universe["kickoff_utc"], utc=True)
    columns = ["game_id", "season", "week", "kickoff_utc", "prediction_cutoff_utc", "team", "opponent", "player_id", "player_name", "position", "status", "status_eligible", "rookie_year", "years_exp", "universe_rule", "universe_reliability"]
    return universe[columns].reset_index(drop=True)


def load_lagged_snap_counts(settings: Settings, rosters: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for season in settings.training_seasons:
        path = external_root(settings) / f"snap_counts_{season}.csv"
        if path.exists():
            frames.append(_read(path))
    snap = pd.concat(frames, ignore_index=True)
    pfr_map = rosters[["season", "pfr_id", "player_id"]].dropna().drop_duplicates(["season", "pfr_id"])
    snap = snap.merge(pfr_map, left_on=["season", "pfr_player_id"], right_on=["season", "pfr_id"], how="left")
    for column in ("offense_snaps", "offense_pct"):
        snap[column] = pd.to_numeric(snap[column], errors="coerce")
    snap["snap_reliability"] = "EXACT_POSTGAME_LAGGED"
    return snap


def current_schedule(settings: Settings) -> pd.DataFrame:
    games = _read(external_root(settings) / "games.csv")
    games = games[(games["season"].eq(settings.current_season)) & games["game_type"].eq("REG")].copy()
    local = pd.to_datetime(games["gameday"].astype(str) + " " + games["gametime"].fillna("13:00").astype(str), errors="coerce")
    games["kickoff_utc"] = local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    return games
