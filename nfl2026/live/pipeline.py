from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from urllib.parse import urljoin

import pandas as pd
from lxml import html

from nfl2026.config import Settings
from nfl2026.live.providers.base import ProviderResult, fetch_bytes, parse_nflverse_timestamp, utc_iso
from nfl2026.live.providers.depth import load_depth
from nfl2026.live.providers.injuries import load_injuries, merge_official_supplement, quarantine_official_supplement
from nfl2026.live.providers.official_injuries import load_official_injury_tables, inspect_official_page_identity
from nfl2026.live.providers.quarterbacks import load_quarterbacks
from nfl2026.live.providers.qb_evidence import capture_sources, load_evidence, reconcile_quarterbacks
from nfl2026.live.providers.transactions import (
    NFL_TRANSACTION_URL, TRANSACTION_CATEGORIES, inspect_official_transaction_page,
    load_transactions, overlay_transactions_on_roster, parse_official_transaction_pages,
)
from nfl2026.live.providers.weather import load_weather


ASSETS = {
    "games.csv": "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "weekly_roster_2026.csv": "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_2026.csv",
    "weekly_rosters_timestamp.json": "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/timestamp.json",
    "injuries_2026.csv": "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_2026.csv",
    "injuries_timestamp.json": "https://github.com/nflverse/nflverse-data/releases/download/injuries/timestamp.json",
    "depth_charts_2026.csv": "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_2026.csv",
    "depth_timestamp.json": "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/timestamp.json",
    "trades.csv": "https://github.com/nflverse/nflverse-data/releases/download/trades/trades.csv",
    "trades_timestamp.json": "https://github.com/nflverse/nflverse-data/releases/download/trades/timestamp.json",
    "nfl_injuries.html": "https://www.nfl.com/injuries/",
    "snap_counts_2026.csv": "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_2026.csv",
}

OPTIONAL_ASSETS = {"snap_counts_2026.csv"}


def normalize_schedule_kickoffs(
    frame: pd.DataFrame,
    required_game_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Return a schedule with canonical UTC kickoffs derived from NFL local schedule fields.

    NFLverse ``gameday``/``gametime`` are the source of truth.  This shared
    normalizer intentionally does not use retrieval or wall-clock timestamps.
    Callers can require a subset of games to have unambiguous, parseable times;
    unresolved required games fail closed instead of receiving a guessed time.
    """
    required_columns = {"game_id", "gameday", "gametime"}
    missing_columns = required_columns - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Schedule lacks canonical kickoff fields: {sorted(missing_columns)}")
    if frame.game_id.astype(str).duplicated().any():
        raise ValueError("Schedule contains duplicate game IDs")

    normalized = frame.copy()
    local = pd.to_datetime(
        normalized.gameday.astype("string") + " " + normalized.gametime.astype("string"),
        format="%Y-%m-%d %H:%M", errors="coerce",
    )
    normalized["kickoff_utc"] = local.dt.tz_localize(
        "America/New_York", ambiguous="NaT", nonexistent="shift_forward"
    ).dt.tz_convert("UTC")

    if required_game_ids is not None:
        required = {str(game_id) for game_id in required_game_ids}
        available = set(normalized.game_id.astype(str))
        missing_ids = required - available
        if missing_ids:
            raise ValueError(f"Schedule is missing required game IDs: {sorted(missing_ids)}")
        unresolved = normalized.loc[
            normalized.game_id.astype(str).isin(required) & normalized.kickoff_utc.isna(), "game_id"
        ].astype(str).tolist()
        if unresolved:
            raise ValueError(f"Canonical scheduled kickoff is missing or malformed for games: {sorted(unresolved)}")
    return normalized


def _schedule_with_kickoff(frame: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    target = frame[(frame.season.eq(season)) & (frame.week.eq(week)) & frame.game_type.eq("REG")].copy()
    if target.empty:
        raise ValueError(f"No regular-season schedule games for {season} Week {week}")
    return normalize_schedule_kickoffs(target, required_game_ids=set(target.game_id.astype(str)))


def _roster_for_week(frame: pd.DataFrame, season: int, week: int, teams: set[str]) -> tuple[pd.DataFrame, str, list[str]]:
    exact=frame[(frame.season.eq(season))&frame.week.eq(week)&frame.team.isin(teams)].copy()
    if set(exact.team)==teams:
        exact["roster_source_week"]=week
        exact["roster_resolution"]="EXACT_WEEK"
        return exact,"READY",[]
    eligible=frame[(frame.season.eq(season))&frame.week.le(week)&frame.team.isin(teams)].copy()
    if eligible.empty: raise ValueError("No current-season roster state is available")
    source_week=int(eligible.week.max())
    latest=eligible[eligible.week.eq(source_week)].copy()
    missing=teams-set(latest.team)
    if missing: raise ValueError(f"Latest roster fallback missing teams: {sorted(missing)}")
    latest["roster_source_week"]=source_week
    latest["roster_resolution"]="LATEST_CURRENT_SEASON_FALLBACK"
    latest["week"]=week
    warning=f"exact Week {week} roster unavailable; rolled forward complete Week {source_week} state and reconciled with current depth/injuries/QBs"
    return latest,"READY_WITH_WARNINGS",[warning]


def download_live_sources(settings: Settings, season: int, retrieved_at: datetime | None = None, *, week: int | None = None) -> Path:
    """Download raw source assets into a new immutable retrieval directory."""
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    stamp = retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    destination = settings.root/"data"/"live_downloads"/str(season)/stamp
    if destination.exists():
        raise FileExistsError(f"Live download is immutable: {destination}")
    destination.mkdir(parents=True)
    failures: list[dict] = []
    required_failures: list[dict] = []
    for filename, template in ASSETS.items():
        url = template.replace("2026", str(season))
        if filename=='nfl_injuries.html' and week is not None:
            url=f'https://www.nfl.com/injuries/league/{season}/reg{week}'
        try:
            payload = fetch_bytes(url)
            with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
                stream.write(payload)
                temporary = Path(stream.name)
            temporary.replace(destination/filename.replace("2026", str(season)))
        except Exception as exc:
            message = str(exc)
            source_state = "NOT_YET_PUBLISHED" if "404" in message or "Not Found" in message else "ERROR"
            failure = {"file":filename,"url":url,"source_state":source_state,"required":filename not in OPTIONAL_ASSETS,"error":message}
            failures.append(failure)
            if filename not in OPTIONAL_ASSETS:
                required_failures.append(failure)
    try:
        capture_sources(settings, season, destination, retrieved_at, fetch_bytes, week=week)
        from nfl2026.live.providers.club_transactions import capture_sources as capture_club_transactions
        capture_club_transactions(settings, season, destination, retrieved_at, fetch_bytes)
        from nfl2026.live.providers.official_roster_identity import capture_sources as capture_official_rosters
        capture_official_rosters(settings,season,destination,retrieved_at,fetch_bytes,week=week)
    except Exception as exc:
        failure = {"file":"qb_evidence_sources.json", "source_state":"ERROR", "required":True, "error":str(exc)}
        failures.append(failure)
        required_failures.append(failure)
    month = retrieved_at.month
    from nfl2026.live.providers.club_availability import capture_sources as capture_availability
    try:
        capture_availability(settings,season,destination,retrieved_at,fetch_bytes,week=week)
    except Exception as exc:
        failure={"file":"availability_sources.json","source_state":"ERROR","required":True,"error":str(exc)}
        failures.append(failure)
        required_failures.append(failure)
    transaction_categories = []
    for category in TRANSACTION_CATEGORIES:
        url = NFL_TRANSACTION_URL.format(category=category, season=season, month=month)
        visited = set()
        page_hashes = set()
        category_pages = 0
        category_rows = 0
        category_state = "VALID"
        pagination_complete = True
        for page_number in range(1, 26):
            if url in visited:
                break
            visited.add(url)
            filename = f"nfl_transactions_{category}_{month:02d}_page{page_number:02d}.html"
            try:
                payload = fetch_bytes(url)
                tree, page_state = inspect_official_transaction_page(payload, url, category, season, month)
                digest = hashlib.sha256(payload).hexdigest()
                if digest in page_hashes:
                    pagination_complete = False
                    failures.append({"file":filename,"url":url,"source_state":"ERROR","required":False,"error":"duplicate page content while following official transaction pagination"})
                    break
                page_hashes.add(digest)
                (destination/filename).write_bytes(payload)
                category_pages += 1
                category_rows += len(parse_official_transaction_pages({filename: payload}, season, retrieved_at))
                if page_state == "VALID_EMPTY":
                    category_state = "VALID_EMPTY"
                    # An explicit empty result is a complete first page, not a
                    # paginated table with more results to follow.
                    break
                next_links = tree.xpath("//a[contains(normalize-space(string(.)), 'Next Page')]/@href")
                if not next_links:
                    break
                if page_number == 25:
                    raise ValueError("official transaction pagination exceeded the 25-page safety limit")
                url = urljoin(url, next_links[0])
            except Exception as exc:
                message = str(exc)
                source_state = "NOT_YET_PUBLISHED" if "404" in message or "Not Found" in message else "ERROR"
                failure = {"file":filename,"url":url,"source_state":source_state,"required":True,"error":message}
                failures.append(failure)
                required_failures.append(failure)
                break
        category_complete = pagination_complete and category_pages > 0 and not any(
            f.get("url", "").find(f"/transactions/league/{category}/") >= 0 for f in failures
        )
        transaction_categories.append({
            "category":category, "month":month, "pages":category_pages,
            "rows":category_rows, "source_state":category_state if category_complete else "ERROR",
            "complete":category_complete,
        })
    (destination/"retrieval.json").write_text(json.dumps({
        "retrieved_at_utc": utc_iso(retrieved_at), "failures": failures,
        "official_transaction_categories": transaction_categories,
        "attempt_policy": "3 attempts with finite timeout and short backoff",
    }, indent=2), encoding="utf-8")
    if required_failures:
        raise RuntimeError(f"Live retrieval incomplete; immutable failure record: {destination/'retrieval.json'}")
    return destination


def _provider_manifest(results: list[ProviderResult], season: int, week: int) -> pd.DataFrame:
    rows = []
    for result in results:
        if result.name in {"injuries","depth","quarterbacks"}:
            status_column="coverage_status" if "coverage_status" in result.coverage else "status"
            covered = int(result.coverage[status_column].isin(["REPORT_PUBLISHED","NOT_YET_PUBLISHED","EXACT_CURRENT_DEPTH","MODEL_INFERRED_FALLBACK","EXPECTED","CONFIRMED","VALID"]).sum())
            denominator = len(result.coverage)
        elif result.name == "weather":
            covered = int(result.coverage.forecast_status.isin(["AVAILABLE","ROOF_SUPPRESSED"]).sum())
            denominator = len(result.coverage)
        else:
            covered, denominator = len(result.records), len(result.records)
        source_state = "VALID"
        if result.name == "injuries" and "source_state" in result.coverage:
            states = set(result.coverage.source_state.astype(str))
            source_state = "ERROR" if "ERROR" in states else "STALE" if "STALE" in states else "SOURCE_MISSING" if "SOURCE_MISSING" in states else "NOT_YET_PUBLISHED" if states == {"NOT_YET_PUBLISHED"} else "VALID"
        elif result.name == "quarterbacks" and "source_state" in result.coverage:
            states=set(result.coverage.source_state.astype(str))
            source_state="ERROR" if "ERROR" in states else "STALE" if "STALE" in states else "SOURCE_MISSING" if "SOURCE_MISSING" in states else "VALID"
        elif result.name == "snap_history":
            source_state = "VALID" if result.status == "READY" else result.status
            covered, denominator = (1, 1) if result.status == "READY" else (0, 1)
        rows.append({
            "source":result.name, "season":season, "week":week, "source_url":result.source_url,
            "retrieved_at_utc":result.retrieved_at_utc, "source_known_at_utc":result.source_known_at_utc,
            "source_known_at_basis":result.source_known_at_basis, "data_status":result.status,
            "reliability":result.reliability, "source_state":source_state,
            "coverage_numerator":covered,
            "coverage_denominator":denominator, "quality_warnings":"; ".join(result.warnings),
        })
    return pd.DataFrame(rows)


def stage_live_sources(
    settings: Settings,
    season: int,
    week: int,
    raw_dir: Path,
    retrieved_at: datetime | None = None,
    weather_http_get=None,
) -> dict[str, ProviderResult]:
    """Normalise a raw retrieval and stage current inputs for the immutable snapshot system."""
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    raw_dir = Path(raw_dir)
    raw_schedule = pd.read_csv(raw_dir/"games.csv", low_memory=False)
    schedule = _schedule_with_kickoff(raw_schedule, season, week)
    if schedule.empty or not schedule.game_id.is_unique:
        raise ValueError("Current schedule is empty or contains duplicate game IDs")
    if schedule[["home_score","away_score"]].notna().any().any():
        raise ValueError("Current target week already contains results")
    teams = set(schedule.home_team)|set(schedule.away_team)
    raw_roster = pd.read_csv(raw_dir/f"weekly_roster_{season}.csv", low_memory=False)
    roster,roster_status,roster_warnings = _roster_for_week(raw_roster,season,week,teams)
    depth = load_depth(
        pd.read_csv(raw_dir/f"depth_charts_{season}.csv", low_memory=False), teams,
        retrieved_at, retrieved_at, season,
    )
    roster["roster_original_team"]=roster.team
    depth_team=depth.records.dropna(subset=["player_id"]).drop_duplicates("player_id",keep="last").set_index("player_id").team.to_dict()
    resolved=roster.gsis_id.astype(str).map(depth_team)
    changed=resolved.notna()&resolved.ne(roster.team)
    roster.loc[changed,"team"]=resolved[changed]
    roster["team_reconciled_from_current_depth"]=changed
    if changed.any():
        roster_warnings.append(f"{int(changed.sum())} player-team states reconciled from point-in-time current depth")

    from nfl2026.live.providers.official_roster_identity import merge_sources as merge_official_rosters, reconcile_explicit_negative_roster
    roster=merge_official_rosters(raw_dir,roster,retrieved_at)
    injuries = load_injuries(
        pd.read_csv(raw_dir/f"injuries_{season}.csv", low_memory=False), schedule, season, week,
        parse_nflverse_timestamp(raw_dir/"injuries_timestamp.json"), retrieved_at,
    )
    official_path=raw_dir/"nfl_injuries.html"
    official_rejection = None
    official_route = None
    if official_path.exists():
        official_payload = official_path.read_bytes()
        try:
            official=load_official_injury_tables(official_payload,roster,season,week,retrieved_at)
            injuries=merge_official_supplement(injuries,official,retrieved_at)
        except ValueError as exc:
            identity=inspect_official_page_identity(official_payload,season,week)
            if identity['state']=='PREVIOUS_WEEK_QUARANTINED':
                # The league endpoint has not advanced. Only independent
                # current-week reports/timetable assessment may supply coverage.
                official_route={**identity,'source_sha256':hashlib.sha256(official_payload).hexdigest()}
            else:
                official_rejection = (str(exc), hashlib.sha256(official_payload).hexdigest())
    from nfl2026.live.providers.club_availability import load_sources as load_availability
    from nfl2026.live.availability import apply_current_sources
    availability_events,availability_coverage=load_availability(raw_dir,roster,schedule,retrieved_at)
    injuries=apply_current_sources(injuries,availability_events,availability_coverage,schedule,retrieved_at)
    if official_route is not None:
        from dataclasses import replace
        for frame in (injuries.records,injuries.coverage):
            frame['official_league_route_state']=official_route['state']
            frame['official_league_observed_week']=official_route['observed_week']
            frame['official_league_rejected_source_sha256']=official_route['source_sha256']
        injuries=replace(injuries,warnings=[*injuries.warnings,
            'Official league endpoint still serves previous week; zero league rows accepted. Coverage requires independent current-week evidence or a not-yet-due state.'])
    if official_rejection is not None:
        # Apply last so a later supplement cannot silently clear this failure.
        injuries = quarantine_official_supplement(injuries, *official_rejection)
    official_transaction_pages = {path.name:path.read_bytes() for path in raw_dir.glob("nfl_transactions_*_page*.html")}
    retrieval_meta=json.loads((raw_dir/"retrieval.json").read_text(encoding="utf-8")) if (raw_dir/"retrieval.json").exists() else {}
    complete_transaction_categories={row["category"] for row in retrieval_meta.get("official_transaction_categories",[]) if row.get("complete")}
    from nfl2026.live.providers.club_transactions import load_sources as load_club_transactions
    club_transactions = load_club_transactions(raw_dir, season, retrieved_at)
    transactions = load_transactions(
        pd.read_csv(raw_dir/"trades.csv", low_memory=False), season, retrieved_at,
        parse_nflverse_timestamp(raw_dir/"trades_timestamp.json"), retrieved_at, official_transaction_pages, complete_transaction_categories, club_transactions,
    )
    roster, transaction_overlay = overlay_transactions_on_roster(roster, transactions.records, raw_schedule,
        cutoff=retrieved_at, roster_known_at=parse_nflverse_timestamp(raw_dir/"weekly_rosters_timestamp.json"))
    roster=reconcile_explicit_negative_roster(roster,retrieved_at)
    if transaction_overlay["matched"]:
        transaction_inactive = int(roster.current_transaction_state.astype(str).str.upper().isin(["IR","RELEASED","PRACTICE_SQUAD"]).sum())
        roster_warnings.append(
            f"current authoritative transactions overlaid on {transaction_overlay['matched']} roster row(s); "
            f"IR/released/practice-squad exclusions={transaction_inactive}"
        )
    if transaction_overlay["unmatched_player_status"]:
        roster_warnings.append(f"{transaction_overlay['unmatched_player_status']} current eligibility transactions could not be matched to roster identity")
    unresolved=roster[roster.transaction_reconciliation_error.fillna('').ne('')]
    if len(unresolved):
        roster_warnings.append(f"{len(unresolved)} roster transaction contradictions retained; production blocks any affected offensive candidate: " +
            json.dumps(unresolved[['gsis_id','full_name','position','team','transaction_reconciliation_error']].to_dict('records')))
        roster_status='READY_WITH_WARNINGS'
    evidence = load_evidence(raw_dir, roster, raw_schedule, retrieved_at)
    depth = load_depth(pd.read_csv(raw_dir/f'depth_charts_{season}.csv',low_memory=False),teams,retrieved_at,retrieved_at,season,
        roster=roster,injuries=injuries.records,transactions=transactions.records,transaction_coverage=transactions.coverage.assign(known_at_utc=transactions.source_known_at_utc,retrieved_at_utc=transactions.retrieved_at_utc))
    schedule = reconcile_quarterbacks(schedule, roster, depth.records, injuries.records,
                                     week, retrieved_at, evidence)
    qbs = load_quarterbacks(schedule, roster, depth.records, week, retrieved_at, injuries.records)
    stadiums = pd.read_csv(settings.legacy_root/"2025 Stats"/"stadiums_2025.csv")
    weather = load_weather(schedule, stadiums, retrieved_at, weather_http_get)
    results = [injuries, depth, qbs, transactions, weather]

    # Snap share is useful but non-critical to the fitted player pipelines:
    # their saved median imputer accepts NA. Preserve an explicit source state
    # and never manufacture a missing snap percentage.
    snap_path = raw_dir/f"snap_counts_{season}.csv"
    if snap_path.exists():
        snap_records = pd.read_csv(snap_path, low_memory=False)
        required_snap = {"season", "game_id", "pfr_player_id", "offense_snaps", "offense_pct"}
        if not required_snap.issubset(snap_records.columns):
            raise ValueError(f"Snap-count source missing fields: {sorted(required_snap-set(snap_records.columns))}")
        results.append(ProviderResult("snap_history", snap_records, pd.DataFrame(), "READY",
            ASSETS["snap_counts_2026.csv"].replace("2026", str(season)), utc_iso(retrieved_at),
            utc_iso(retrieved_at), "retrieval time; nflverse season file has no embedded release timestamp",
            "EXACT_POSTGAME_LAGGED", []))
    else:
        retrieval_state = {}
        retrieval_path = raw_dir/"retrieval.json"
        if retrieval_path.exists():
            retrieval_state = json.loads(retrieval_path.read_text(encoding="utf-8"))
        failure = next((item for item in retrieval_state.get("failures", []) if item.get("file") == f"snap_counts_{season}.csv"), None)
        state = failure.get("source_state", "SOURCE_MISSING") if failure else "SOURCE_MISSING"
        results.append(ProviderResult("snap_history", pd.DataFrame(), pd.DataFrame(), state,
            ASSETS["snap_counts_2026.csv"].replace("2026", str(season)), utc_iso(retrieved_at),
            utc_iso(retrieved_at), "nflverse release check", "EXACT_POSTGAME_LAGGED",
            [failure.get("error", "") for _ in [0] if failure] or []))

    target = settings.root/"data"/"phase2_external"
    target.mkdir(parents=True, exist_ok=True)
    for row in schedule.itertuples(index=False):
        mask=raw_schedule.game_id.eq(row.game_id)
        for side in ("home","away"):
            raw_schedule.loc[mask,f"{side}_qb_id"]=getattr(row,f"{side}_qb_id")
            raw_schedule.loc[mask,f"{side}_qb_name"]=getattr(row,f"{side}_qb_name")
            raw_schedule.loc[mask,f"{side}_qb_original_id"]=getattr(row,f"{side}_qb_original_id")
            raw_schedule.loc[mask,f"{side}_qb_original_name"]=getattr(row,f"{side}_qb_original_name")
            raw_schedule.loc[mask,f"{side}_qb_resolution"]=getattr(row,f"{side}_qb_resolution")
            for field in ("confirmed", "evidence", "evidence_error"):
                raw_schedule.loc[mask,f"{side}_qb_{field}"]=getattr(row,f"{side}_qb_{field}")
    raw_schedule.to_csv(target/"games.csv", index=False, lineterminator="\n")
    roster.to_csv(target/f"weekly_roster_{season}.csv", index=False, lineterminator="\n")
    names = {
        "injuries":f"injuries_{season}.csv", "depth":f"depth_charts_{season}.csv",
        "quarterbacks":f"qb_status_{season}_w{week:02d}.csv",
        "transactions":f"transactions_{season}.csv", "weather":f"weather_{season}_w{week:02d}.csv",
    }
    for result in results:
        if result.name == "snap_history":
            continue
        result.records.to_csv(target/names[result.name], index=False, lineterminator="\n")
        result.coverage.to_csv(target/f"{result.name}_coverage_{season}_w{week:02d}.csv", index=False, lineterminator="\n")

    if snap_path.exists():
        (target/f"snap_counts_{season}.csv").write_bytes(snap_path.read_bytes())

    roster_known = parse_nflverse_timestamp(raw_dir/"weekly_rosters_timestamp.json")
    schedule_result = ProviderResult(
        "schedule", schedule, pd.DataFrame(), "READY", ASSETS["games.csv"], utc_iso(retrieved_at),
        utc_iso(retrieved_at), "retrieval time; raw GitHub file has no source issue timestamp",
        "CURRENT_STRUCTURED_SCHEDULE", [],
    )
    roster_result = ProviderResult(
        "roster", roster, pd.DataFrame(), roster_status, ASSETS["weekly_roster_2026.csv"].replace("2026",str(season)),
        utc_iso(retrieved_at), utc_iso(roster_known), "nflverse release timestamp",
        "CURRENT_WEEKLY_ROSTER_WITH_LABELLED_FALLBACK", roster_warnings,
    )
    manifest = _provider_manifest([schedule_result, roster_result, *results], season, week)
    manifest.to_csv(target/f"live_provider_manifest_{season}_w{week:02d}.csv", index=False, lineterminator="\n")
    (target/f"live_retrieval_{season}_w{week:02d}.json").write_text(json.dumps({
        "retrieved_at_utc":utc_iso(retrieved_at), "raw_directory":str(raw_dir.relative_to(settings.root)),
        "transaction_overlay":transaction_overlay,
        "providers":manifest.to_dict("records"),
    }, indent=2), encoding="utf-8")
    return {result.name:result for result in results}
