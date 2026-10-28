from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.data.manifest import canonical_source
from nfl2026.utilities.io import write_csv_json


def _read(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path, columns=columns)
    frame = pd.read_csv(path, low_memory=False)
    return frame if columns is None else frame[[c for c in columns if c in frame.columns]]


def _kickoff_utc(frame: pd.DataFrame) -> pd.Series:
    local = pd.to_datetime(
        frame["gameday"].astype(str) + " " + frame["gametime"].fillna("13:00").astype(str),
        errors="coerce",
    )
    return local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")


def build_canonical_games(settings: Settings, manifest: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    columns = [
        "game_id", "season", "game_type", "week", "gameday", "gametime", "away_team",
        "away_score", "home_team", "home_score", "location", "roof", "surface", "stadium_id", "stadium",
    ]
    for season in settings.training_seasons:
        path = canonical_source(manifest, season, "schedules", settings)
        if path is None:
            continue
        frame = _read(path)
        frame = frame[[c for c in columns if c in frame.columns]].copy()
        frame["kickoff_utc"] = _kickoff_utc(frame)
        frame["result_status"] = np.where(frame["home_score"].notna() & frame["away_score"].notna(), "completed", "scheduled")
        frame["home_win"] = np.where(frame["result_status"].eq("completed"), (frame["home_score"] > frame["away_score"]).astype(float), np.nan)
        frame["margin"] = frame["home_score"] - frame["away_score"]
        frame["game_total"] = frame["home_score"] + frame["away_score"]
        frames.append(frame)
    games = pd.concat(frames, ignore_index=True).sort_values(["kickoff_utc", "game_id"]).reset_index(drop=True)
    write_csv_json(games, settings.artifact_root / "canonical" / "canonical_games.csv")
    return games


PBP_COLUMNS = [
    "game_id", "season", "season_type", "week", "game_date", "posteam", "defteam", "yardline_100",
    "rush_attempt", "pass_attempt", "complete_pass", "qb_kneel", "qb_spike", "pass_touchdown", "rush_touchdown",
    "td_player_id", "td_player_name",
    "receiver_player_id", "receiver_player_name", "receiving_yards", "air_yards",
    "rusher_player_id", "rusher_player_name", "rushing_yards",
]


def aggregate_player_pbp(settings: Settings, manifest: pd.DataFrame) -> pd.DataFrame:
    receiver_frames: list[pd.DataFrame] = []
    rusher_frames: list[pd.DataFrame] = []
    receiving_td_frames: list[pd.DataFrame] = []
    rushing_td_frames: list[pd.DataFrame] = []
    group = ["game_id", "season", "week", "team", "opponent", "player_id"]
    for season in settings.training_seasons:
        path = canonical_source(manifest, season, "pbp", settings)
        if path is None:
            continue
        raw = _read(path, PBP_COLUMNS)
        raw = raw[raw["season_type"].eq("REG")].copy()
        for column in (
            "rush_attempt", "pass_attempt", "complete_pass", "qb_kneel", "qb_spike", "pass_touchdown",
            "rush_touchdown", "receiving_yards", "rushing_yards", "air_yards", "yardline_100",
        ):
            raw[column] = pd.to_numeric(raw.get(column, 0), errors="coerce").fillna(0)

        rec = raw[raw["receiver_player_id"].notna()].copy()
        rec = rec.rename(columns={"posteam": "team", "defteam": "opponent", "receiver_player_id": "player_id", "receiver_player_name": "player_name"})
        rec["targets"] = (rec["pass_attempt"] == 1).astype(int)
        rec["receptions"] = (rec["complete_pass"] == 1).astype(int)
        rec["red_zone_targets"] = ((rec["pass_attempt"] == 1) & rec["yardline_100"].between(1, 20)).astype(int)
        rec["inside_10_targets"] = ((rec["pass_attempt"] == 1) & rec["yardline_100"].between(1, 10)).astype(int)
        rec["inside_5_targets"] = ((rec["pass_attempt"] == 1) & rec["yardline_100"].between(1, 5)).astype(int)
        rec_stats = rec.groupby(group, dropna=False, as_index=False)[
            ["targets", "receptions", "receiving_yards", "air_yards", "red_zone_targets", "inside_10_targets", "inside_5_targets"]
        ].sum()
        rec_names = rec.groupby(group, dropna=False, as_index=False)["player_name"].last()
        receiver_frames.append(rec_stats.merge(rec_names, on=group, how="left"))

        rush = raw[raw["rusher_player_id"].notna()].copy()
        rush = rush.rename(columns={"posteam": "team", "defteam": "opponent", "rusher_player_id": "player_id", "rusher_player_name": "player_name"})
        valid_carry = (rush["rush_attempt"] == 1) & (rush["qb_kneel"] != 1) & (rush["qb_spike"] != 1)
        rush["carries"] = valid_carry.astype(int)
        rush["red_zone_carries"] = (valid_carry & rush["yardline_100"].between(1, 20)).astype(int)
        rush["inside_10_carries"] = (valid_carry & rush["yardline_100"].between(1, 10)).astype(int)
        rush["inside_5_carries"] = (valid_carry & rush["yardline_100"].between(1, 5)).astype(int)
        rush.loc[~valid_carry, "rushing_yards"] = 0
        rush_stats = rush.groupby(group, dropna=False, as_index=False)[
            ["carries", "rushing_yards", "red_zone_carries", "inside_10_carries", "inside_5_carries"]
        ].sum()
        rush_names = rush.groupby(group, dropna=False, as_index=False)["player_name"].last()
        rusher_frames.append(rush_stats.merge(rush_names, on=group, how="left"))

        # On lateral touchdowns the scorer is td_player_id, not necessarily the initial receiver/rusher.
        for td_type, output in (("pass_touchdown", receiving_td_frames), ("rush_touchdown", rushing_td_frames)):
            td = raw[(raw[td_type] == 1) & raw["td_player_id"].notna()].copy()
            td = td.rename(columns={"posteam": "team", "defteam": "opponent", "td_player_id": "player_id", "td_player_name": "player_name"})
            td["touchdown_count"] = 1
            td_counts = td.groupby(group, dropna=False, as_index=False)["touchdown_count"].sum()
            td_names = td.groupby(group, dropna=False, as_index=False)["player_name"].last()
            output.append(td_counts.merge(td_names, on=group, how="left"))

    receivers = pd.concat(receiver_frames, ignore_index=True) if receiver_frames else pd.DataFrame(columns=group)
    rushers = pd.concat(rusher_frames, ignore_index=True) if rusher_frames else pd.DataFrame(columns=group)
    pbp = receivers.merge(rushers, on=group, how="outer", suffixes=("_receiving", "_rushing"))
    pbp["player_name"] = pbp.get("player_name_receiving").fillna(pbp.get("player_name_rushing"))
    pbp = pbp.drop(columns=[c for c in ("player_name_receiving", "player_name_rushing") if c in pbp])
    for frames, column in ((receiving_td_frames, "receiving_tds"), (rushing_td_frames, "rushing_tds")):
        td_all = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=[*group, "touchdown_count", "player_name"])
        td_all = td_all.groupby(group, as_index=False).agg(touchdown_count=("touchdown_count", "sum"), touchdown_player_name=("player_name", "last"))
        pbp = pbp.merge(td_all, on=group, how="outer")
        pbp["player_name"] = pbp["player_name"].fillna(pbp["touchdown_player_name"])
        pbp = pbp.rename(columns={"touchdown_count": column}).drop(columns="touchdown_player_name")
    stat_columns = [c for c in pbp.columns if c not in [*group, "player_name"]]
    pbp[stat_columns] = pbp[stat_columns].fillna(0)
    pbp["anytime_offensive_tds"] = pbp["receiving_tds"] + pbp["rushing_tds"]
    pbp["anytime_offensive_td"] = (pbp["anytime_offensive_tds"] > 0).astype(int)
    pbp = pbp.sort_values(["season", "week", "game_id", "team", "player_id"]).reset_index(drop=True)
    write_csv_json(pbp, settings.artifact_root / "canonical" / "player_pbp_weekly.csv")
    return pbp


def build_player_weekly(settings: Settings, manifest: pd.DataFrame, pbp: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for season in settings.training_seasons:
        path = canonical_source(manifest, season, "weekly", settings)
        if path is None:
            continue
        source = _read(path)
        if "season_type" in source:
            source = source[source["season_type"].eq("REG")]
        if "recent_team" in source:
            source = source.rename(columns={"recent_team": "team", "player_display_name": "display_name"})
        else:
            source = source.rename(columns={"player_name": "display_name"})
        source["season"] = season
        desired = [
            "season", "week", "team", "opponent_team", "player_id", "display_name", "position",
            "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards", "receiving_air_yards", "air_yards", "receiving_tds",
        ]
        source = source[[c for c in desired if c in source]].copy()
        if "receiving_air_yards" in source and "air_yards" not in source:
            source = source.rename(columns={"receiving_air_yards": "air_yards"})
        source = source[source["player_id"].notna()].copy()
        frames.append(source)
    weekly = pd.concat(frames, ignore_index=True)
    numeric = ["carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards", "air_yards", "receiving_tds"]
    for column in numeric:
        weekly[column] = pd.to_numeric(weekly.get(column, 0), errors="coerce").fillna(0)

    # The inherited 2025 receiving-TD field is known to be broken; replace all 2025 usage/labels with PBP-derived values.
    pbp_2025 = pbp[pbp["season"].eq(2025)].copy()
    identity = (
        weekly[weekly["season"].eq(2025)][["season", "week", "team", "player_id", "display_name", "position"]]
        .groupby(["season", "week", "team", "player_id"], as_index=False)
        .agg(display_name=("display_name", "last"), position=("position", "last"))
    )
    pbp_2025 = pbp_2025.merge(identity, on=["season", "week", "team", "player_id"], how="left", suffixes=("", "_weekly"))
    pbp_2025["display_name"] = pbp_2025["display_name"].fillna(pbp_2025["player_name"])
    pbp_2025["opponent_team"] = pbp_2025["opponent"]
    pbp_2025 = pbp_2025[[
        "season", "week", "team", "opponent_team", "player_id", "display_name", "position",
        "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards", "air_yards", "receiving_tds",
    ]]
    weekly = pd.concat([weekly[~weekly["season"].eq(2025)], pbp_2025], ignore_index=True)
    weekly["anytime_offensive_tds"] = weekly["rushing_tds"] + weekly["receiving_tds"]
    weekly["anytime_offensive_td"] = (weekly["anytime_offensive_tds"] > 0).astype(int)
    weekly = weekly.sort_values(["season", "week", "team", "player_id"]).reset_index(drop=True)

    players = (
        weekly.sort_values(["season", "week"])
        .groupby("player_id", as_index=False)
        .agg(display_name=("display_name", "last"), primary_position=("position", "last"), first_season=("season", "min"), last_season=("season", "max"))
    )
    write_csv_json(weekly, settings.artifact_root / "canonical" / "player_weekly.csv")
    write_csv_json(players, settings.artifact_root / "canonical" / "canonical_players.csv")
    write_csv_json(
        weekly[["season", "week", "team", "opponent_team", "player_id", "rushing_tds", "receiving_tds", "anytime_offensive_tds", "anytime_offensive_td"]],
        settings.artifact_root / "canonical" / "td_labels.csv",
    )
    return weekly, players


def build_membership(settings: Settings, manifest: pd.DataFrame, games: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    fixture = pd.concat(
        [
            games[["season", "week", "home_team", "kickoff_utc"]].rename(columns={"home_team": "team"}),
            games[["season", "week", "away_team", "kickoff_utc"]].rename(columns={"away_team": "team"}),
        ],
        ignore_index=True,
    ).sort_values("kickoff_utc").drop_duplicates(["season", "week", "team"])
    observed = weekly[["season", "week", "team", "player_id", "display_name", "position"]].drop_duplicates()
    observed = observed.merge(fixture, on=["season", "week", "team"], how="left")
    observed["known_at"] = pd.to_datetime(observed["kickoff_utc"], utc=True) + pd.Timedelta(days=1)
    observed["source"] = "weekly_stats_postgame"
    observed["availability_class"] = "observed_after_game"
    events = [observed.rename(columns={"kickoff_utc": "effective_at"})]

    for season in settings.training_seasons:
        path = canonical_source(manifest, season, "depth_charts", settings)
        if path is None:
            continue
        depth = _read(path)
        if season <= 2024:
            depth = depth.rename(columns={"club_code": "team", "gsis_id": "player_id", "full_name": "display_name", "position": "position"})
            depth = depth[depth["player_id"].notna() & depth["week"].notna()].copy()
            depth["week"] = depth["week"].astype(int)
            depth = depth.merge(fixture, on=["season", "week", "team"], how="left")
            depth["effective_at"] = pd.to_datetime(depth["kickoff_utc"], utc=True) - pd.Timedelta(minutes=1)
            depth["known_at"] = depth["effective_at"]
            depth["source"] = "weekly_depth_chart"
            depth["availability_class"] = "week_snapshot_assumed_pregame"
        else:
            depth = depth.rename(columns={"gsis_id": "player_id", "player_name": "display_name", "pos_abb": "position"})
            depth = depth[depth["player_id"].notna()].copy()
            depth["known_at"] = pd.to_datetime(depth["dt"], errors="coerce", utc=True)
            depth["effective_at"] = depth["known_at"]
            depth["week"] = pd.NA
            depth["season"] = season
            depth["source"] = "timestamped_depth_chart"
            depth["availability_class"] = "timestamped_snapshot"
        events.append(depth[["season", "week", "team", "player_id", "display_name", "position", "effective_at", "known_at", "source", "availability_class"]])

    membership = pd.concat(events, ignore_index=True)
    membership = membership.dropna(subset=["player_id", "team", "known_at"])
    membership = membership.drop_duplicates(["player_id", "team", "known_at", "source"]).sort_values(["player_id", "known_at", "team"])
    # Store association change events, not hundreds of unchanged daily depth snapshots.
    membership = membership[membership["team"].ne(membership.groupby("player_id")["team"].shift(1))].reset_index(drop=True)
    membership["valid_from"] = membership["effective_at"]
    membership["valid_to"] = membership.groupby("player_id")["effective_at"].shift(-1)
    membership["membership_event_id"] = membership.index.map(lambda i: f"membership-{i:09d}")
    write_csv_json(membership, settings.artifact_root / "canonical" / "player_team_membership.csv")
    return membership


def membership_as_of(membership: pd.DataFrame, cutoff: str | pd.Timestamp) -> pd.DataFrame:
    point = pd.Timestamp(cutoff)
    point = point.tz_localize("UTC") if point.tzinfo is None else point.tz_convert("UTC")
    eligible = membership[pd.to_datetime(membership["known_at"], utc=True) < point]
    return eligible.sort_values("known_at").groupby("player_id", as_index=False).tail(1)


def td_reconciliation(settings: Settings, weekly: pd.DataFrame, pbp: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for season in (2022, 2023, 2024):
        source = weekly[weekly["season"].eq(season)]
        derived = pbp[pbp["season"].eq(season)]
        for stat in ("rushing_tds", "receiving_tds", "rushing_yards", "receiving_yards"):
            left, right = float(source[stat].sum()), float(derived[stat].sum())
            relative = abs(left - right) / max(abs(left), 1.0)
            tolerance = settings.td_reconciliation_tolerance if "tds" in stat else 0.02
            rows.append(
                {
                    "season": season,
                    "stat": stat,
                    "weekly_source_total": left,
                    "pbp_derived_total": right,
                    "absolute_difference": abs(left - right),
                    "relative_difference": relative,
                    "tolerance": tolerance,
                    "status": "pass" if relative <= tolerance else "review",
                    "note": "Two 2024 lateral-score plays differ from weekly receiving-TD statistical convention; PBP stable scorer is retained." if season == 2024 and stat == "receiving_tds" and relative else "",
                }
            )
    return pd.DataFrame(rows)
