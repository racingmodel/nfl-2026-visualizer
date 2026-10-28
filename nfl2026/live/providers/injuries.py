from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from nfl2026.live.providers.base import ProviderResult, utc_iso


INJURY_URL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
SOURCE_STATES = {"VALID", "NOT_YET_PUBLISHED", "STALE", "SOURCE_MISSING", "ERROR"}
MAX_INJURY_AGE_HOURS = 36


def quarantine_official_supplement(result: ProviderResult, reason: str, source_sha256: str) -> ProviderResult:
    """Retain independent adverse facts, but never certify a rejected supplement.

    Staging other independent sources may finish for diagnosis/preparation.
    Both provider-level and per-team production gates remain blocked, including
    before the first practice-report deadline.
    """
    coverage = result.coverage.copy()
    records = result.records.copy()
    for frame in (coverage, records):
        for column in ("coverage_status", "source_state"):
            if column in frame:
                frame["before_official_rejection_" + column] = frame[column]
                frame[column] = "ERROR"
        frame["official_rejection_reason"] = reason
        frame["official_rejected_source_sha256"] = source_sha256
    return replace(result, records=records, coverage=coverage, status="BLOCKED",
                   warnings=[*result.warnings,
                             "Official injury supplement quarantined; current-week identity or format was not verified: " + reason])


def normalise_practice(value: object) -> str:
    text = str(value or "").strip().upper()
    if "DID NOT" in text or text == "DNP":
        return "DNP"
    if "LIMITED" in text:
        return "LIMITED"
    if "FULL" in text:
        return "FULL"
    return "UNKNOWN"


def normalise_game_status(value: object) -> str:
    text = str(value or "").strip().upper()
    mapping = {
        "OUT": "OUT", "DOUBTFUL": "DOUBTFUL", "QUESTIONABLE": "QUESTIONABLE",
        "IR": "IR", "PUP": "PUP", "INACTIVE": "INACTIVE", "INACT": "INACTIVE",
        "ACTIVE": "ACTIVE", "HEALTHY": "HEALTHY",
    }
    return mapping.get(text, "UNKNOWN")


def first_practice_report_due(kickoff_utc: pd.Timestamp) -> datetime:
    """NFL normal first practice-report deadline for a game day, in New York time."""
    local = kickoff_utc.tz_convert("America/New_York")
    # Sunday->Wednesday, Monday->Thursday, Thursday->Monday, Saturday->Tuesday.
    days_before = {6: 4, 0: 4, 3: 3, 5: 4}.get(local.weekday(), 3)
    due_date = (local - pd.Timedelta(days=days_before)).date()
    return datetime.combine(due_date, time(16, 0), ZoneInfo("America/New_York")).astimezone(timezone.utc)


def load_injuries(
    raw: pd.DataFrame,
    schedule: pd.DataFrame,
    season: int,
    week: int,
    source_known_at: datetime,
    retrieved_at: datetime,
) -> ProviderResult:
    target = raw[(raw.season.eq(season)) & (raw.week.eq(week))].copy()
    target["player_id"] = target.get("gsis_id", pd.Series(index=target.index, dtype=object))
    target["player_name"] = target.get("full_name", pd.Series(index=target.index, dtype=object))
    target["original_report_status"] = target.get("report_status", "")
    target["original_practice_status"] = target.get("practice_status", "")
    target["game_status"] = target["original_report_status"].map(normalise_game_status)
    target["practice_status_normalized"] = target["original_practice_status"].map(normalise_practice)
    target["normalized_status"] = target["game_status"].where(target.game_status.ne("UNKNOWN"), target.practice_status_normalized)
    target["injury"] = target.get("practice_primary_injury", "")
    age_hours = (retrieved_at - source_known_at).total_seconds() / 3600
    source_error = source_known_at > retrieved_at
    source_stale = age_hours > MAX_INJURY_AGE_HOURS
    target["known_at_utc"] = utc_iso(source_known_at)
    target["retrieved_at_utc"] = utc_iso(retrieved_at)
    target["source"] = "nflverse injuries release"
    target["source_known_at_basis"] = "nflverse release timestamp; row publication time unavailable"
    columns = [
        "season", "week", "team", "player_id", "player_name", "position", "game_status",
        "practice_status_normalized", "normalized_status", "injury", "original_report_status",
        "original_practice_status", "known_at_utc", "retrieved_at_utc", "source", "source_known_at_basis",
    ]
    records = target.reindex(columns=columns).reset_index(drop=True)

    game_rows: list[dict] = []
    for game in schedule.itertuples(index=False):
        kickoff = pd.Timestamp(game.kickoff_utc)
        if kickoff.tzinfo is None:
            kickoff = kickoff.tz_localize("UTC")
        for team in (game.away_team, game.home_team):
            count = int(records.team.eq(team).sum())
            due = first_practice_report_due(kickoff)
            if count:
                status = "REPORT_PUBLISHED"
                source_state = "VALID"
            elif retrieved_at < due:
                status = "NOT_YET_PUBLISHED"
                source_state = "NOT_YET_PUBLISHED"
            else:
                status = "SOURCE_MISSING"
                source_state = "SOURCE_MISSING"
            if source_error:
                status = "ERROR"
                source_state = "ERROR"
            elif source_stale:
                status = "STALE"
                source_state = "STALE"
            game_rows.append({
                "team": team, "game_id": game.game_id, "kickoff_utc": kickoff.isoformat(),
                "records": count, "coverage_status": status, "source_state": source_state,
                "first_report_due_utc": due.isoformat(),
                "latest_report_known_at_utc": utc_iso(source_known_at) if count else "",
                "unresolved_player_ids": int(records[records.team.eq(team)].player_id.isna().sum()),
                "report_empty": False,
                "source_age_hours": round((retrieved_at-source_known_at).total_seconds()/3600, 2),
            })
    coverage = pd.DataFrame(game_rows).sort_values("team").reset_index(drop=True)
    missing = int(coverage.source_state.isin(["SOURCE_MISSING", "ERROR", "STALE"]).sum())
    not_yet = int(coverage.source_state.eq("NOT_YET_PUBLISHED").sum())
    status = "BLOCKED" if missing else ("READY_WITH_WARNINGS" if not_yet else "READY")
    warnings = []
    if not_yet:
        warnings.append(f"{not_yet}/32 teams verified before their normal first practice-report deadline")
    if missing:
        states = ", ".join(f"{state}={int(coverage.source_state.eq(state).sum())}" for state in ("SOURCE_MISSING", "STALE", "ERROR") if coverage.source_state.eq(state).any())
        warnings.append(f"injury source gate failed for {missing}/32 teams ({states})")
    records["record_type"] = "PLAYER_REPORT"
    coverage_columns = ["team", "coverage_status", "source_state", "records", "first_report_due_utc",
                        "latest_report_known_at_utc", "unresolved_player_ids", "report_empty", "source_age_hours"]
    records = records.merge(coverage[coverage_columns], on="team", how="left")
    records["availability_status"] = records.game_status.where(records.game_status.ne("UNKNOWN"), "UNKNOWN")
    placeholders = coverage.copy()
    placeholders["season"] = season
    placeholders["week"] = week
    placeholders["record_type"] = "TEAM_COVERAGE"
    placeholders["known_at_utc"] = placeholders.latest_report_known_at_utc.where(
        placeholders.latest_report_known_at_utc.astype(str).ne(""), utc_iso(retrieved_at)
    )
    placeholders["retrieved_at_utc"] = utc_iso(retrieved_at)
    placeholders["source"] = "NFL report schedule assessment"
    placeholders["source_known_at_basis"] = "retrieval time and published NFL reporting timetable"
    records = pd.concat([records, placeholders.reindex(columns=records.columns)], ignore_index=True)
    return ProviderResult(
        name="injuries", records=records, coverage=coverage, status=status,
        source_url=INJURY_URL.format(season=season), retrieved_at_utc=utc_iso(retrieved_at),
        source_known_at_utc=utc_iso(source_known_at),
        source_known_at_basis="nflverse release timestamp; underlying API row time not exposed",
        reliability="STRUCTURED_CURRENT_REPORTS", warnings=warnings,
    )


def merge_official_supplement(result: ProviderResult, official: pd.DataFrame, retrieved_at: datetime) -> ProviderResult:
    if official.empty:
        return result
    official = official.copy()
    if "record_type" in official:
        official_players = official[official.record_type.eq("PLAYER_REPORT")].copy()
        official_coverage = official[official.record_type.eq("TEAM_COVERAGE")].copy()
    else:
        # Backward-compatible support for a pre-coverage parser frame.
        official_players = official.copy()
        official_coverage = pd.DataFrame()
    base_players = result.records[result.records.record_type.eq("PLAYER_REPORT")].copy()
    if len(official_players):
        base_players["provider_priority"] = 0
        official_players["provider_priority"] = 1
        players = pd.concat([base_players, official_players], ignore_index=True)
        # A blank game-status cell is practice evidence, never a clearance of a
        # positive adverse designation from another current source.
        adverse={"OUT","INACTIVE","IR","PUP","SUSPENDED","QUESTIONABLE","DOUBTFUL"}
        players["adverse_priority"]=players.game_status.map({s:2 if s in {"QUESTIONABLE","DOUBTFUL"} else 3 for s in adverse}).fillna(0)
        players["name_key"] = players.player_name.astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
        players = players.sort_values(["adverse_priority","provider_priority"]).drop_duplicates(["team", "name_key"], keep="last").drop(columns=["provider_priority", "adverse_priority", "name_key"])
    else:
        players = base_players

    coverage = result.coverage.copy()
    if len(official_coverage):
        if official_coverage.team.astype(str).duplicated().any():
            raise ValueError("official NFL injury coverage contains duplicate team rows")
        official_by_team = official_coverage.set_index(official_coverage.team.astype(str))
        for index, row in coverage.iterrows():
            team = str(row.team)
            if team not in official_by_team.index:
                continue
            official_row = official_by_team.loc[team]
            # Only identity-validated table sections and exact explicit-empty
            # markers are emitted by the official parser as valid coverage.
            if str(official_row.get("source_state", "")) != "VALID":
                continue
            coverage.loc[index, "coverage_status"] = "REPORT_PUBLISHED"
            coverage.loc[index, "source_state"] = "VALID"
            coverage.loc[index, "records"] = int(official_row.get("records", 0))
            coverage.loc[index, "report_empty"] = bool(official_row.get("report_empty", False))
            coverage.loc[index, "latest_report_known_at_utc"] = str(official_row.get("latest_report_known_at_utc", utc_iso(retrieved_at)))
            coverage.loc[index, "unresolved_player_ids"] = int(official_row.get("unresolved_player_ids", 0))
            coverage.loc[index, "source_age_hours"] = 0.0
        # Non-target league teams can appear on the league-wide official page;
        # they must not be added to this game's schedule coverage.

    placeholders = coverage.copy()
    placeholders["season"] = int(result.records.season.dropna().iloc[0]) if len(result.records) and result.records.season.notna().any() else None
    placeholders["week"] = int(result.records.week.dropna().iloc[0]) if len(result.records) and result.records.week.notna().any() else None
    placeholders["record_type"] = "TEAM_COVERAGE"
    placeholders["known_at_utc"] = placeholders.latest_report_known_at_utc.where(
        placeholders.latest_report_known_at_utc.astype(str).ne(""), utc_iso(retrieved_at)
    )
    placeholders["retrieved_at_utc"] = utc_iso(retrieved_at)
    placeholders["source"] = "NFL report schedule assessment"
    placeholders["source_known_at_basis"] = "retrieval time and published NFL reporting timetable"
    if "report_empty" in placeholders:
        empty_teams = set(coverage.loc[coverage.report_empty.astype(bool), "team"].astype(str))
        placeholders.loc[placeholders.team.astype(str).isin(empty_teams), "source"] = "official NFL injury report"
        placeholders.loc[placeholders.team.astype(str).isin(empty_teams), "source_known_at_basis"] = "explicit No Injuries Reported marker; page retrieval time"
    records = pd.concat([players, placeholders.reindex(columns=players.columns)], ignore_index=True)
    missing=int(coverage.source_state.isin(["SOURCE_MISSING","STALE","ERROR"]).sum())
    not_yet=int(coverage.source_state.eq("NOT_YET_PUBLISHED").sum())
    status="BLOCKED" if missing else ("READY_WITH_WARNINGS" if not_yet else "READY")
    warnings=[]
    if not_yet: warnings.append(f"{not_yet}/32 teams verified before their normal first practice-report deadline")
    if missing: warnings.append(f"injury source gate failed for {missing}/32 teams")
    return ProviderResult(
        name=result.name,records=records,coverage=coverage,status=status,
        source_url=f"{result.source_url}; https://www.nfl.com/injuries/",
        retrieved_at_utc=utc_iso(retrieved_at),source_known_at_utc=utc_iso(retrieved_at),
        source_known_at_basis="row source timestamp where available; official supplement retrieval time otherwise",
        reliability="NFLVERSE_PLUS_OFFICIAL_NFL_SUPPLEMENT",warnings=warnings,
    )
