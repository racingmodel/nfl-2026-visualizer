from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from nfl2026.live.pipeline import (
    _provider_manifest, _roster_for_week, _schedule_with_kickoff, normalize_schedule_kickoffs,
)
from nfl2026.live.providers.base import parse_nflverse_timestamp, utc_iso
from nfl2026.live.providers.depth import load_depth
from nfl2026.live.providers.injuries import load_injuries, merge_official_supplement
from nfl2026.live.providers.official_injuries import load_official_injury_tables
from nfl2026.live.providers.quarterbacks import load_quarterbacks
from nfl2026.live.providers.qb_evidence import load_evidence, reconcile_quarterbacks
from nfl2026.live.providers.transactions import load_transactions
from nfl2026.live.providers.weather import load_weather
from nfl2026.utilities.io import sha256_file


def classify_game_eligibility(frame: pd.DataFrame, season: int, week: int, now: datetime) -> pd.DataFrame:
    """Classify games conservatively from source status, scores, and UTC kickoff."""
    target = _schedule_with_kickoff(frame, season, week)
    current = pd.Timestamp(now)
    if current.tzinfo is None:
        raise ValueError("Eligibility time must be timezone-aware")
    current = current.tz_convert("UTC")
    status_cols = [c for c in ("game_status", "status", "game_state", "state") if c in target]
    rows = []
    for game in target.itertuples(index=False):
        status = next((str(getattr(game, c)).strip().upper().replace(" ", "_") for c in status_cols
                       if pd.notna(getattr(game, c)) and str(getattr(game, c)).strip()), "")
        kickoff = pd.Timestamp(game.kickoff_utc)
        both = pd.notna(getattr(game, "away_score", None)) and pd.notna(getattr(game, "home_score", None))
        any_score = pd.notna(getattr(game, "away_score", None)) or pd.notna(getattr(game, "home_score", None))
        if pd.isna(kickoff): state, why = "AMBIGUOUS", "kickoff unavailable"
        elif status in {"FINAL", "FINAL/OT", "FINAL_OT", "COMPLETE", "COMPLETED", "CLOSED"} or both:
            state, why = "FINAL", "final status or both scores present"
        elif status in {"LIVE", "IN_PROGRESS", "INPROGRESS", "HALFTIME", "DELAYED", "SUSPENDED"} or any_score:
            state, why = "LIVE", "live status or partial score present"
        elif status and status not in {"SCHEDULED", "PRE", "PREGAME", "NOT_STARTED", "SCHEDULED_TO_START"}:
            state, why = "AMBIGUOUS", f"unrecognized status {status}"
        elif kickoff <= current:
            state, why = "LIVE", "kickoff reached; conservative immutable classification"
        else:
            state, why = "PRE_KICKOFF", "future kickoff with no live/final signal"
        rows.append({"game_id": str(game.game_id), "season": season, "week": week,
            "kickoff_utc": kickoff.isoformat() if pd.notna(kickoff) else "", "away_team": game.away_team,
            "home_team": game.home_team, "source_status": status, "eligibility_state": state,
            "eligibility_reason": why, "eligible_for_refresh": state == "PRE_KICKOFF", "classified_at_utc": current.isoformat()})
    out = pd.DataFrame(rows)
    if out.empty or out.game_id.duplicated().any():
        raise ValueError("Schedule is empty or game IDs are duplicated")
    return out


def select_partial_prediction_context(
    context: pd.DataFrame,
    eligible_game_ids,
    eligibility: pd.DataFrame,
) -> pd.DataFrame:
    """Validate and select exactly the PRE_KICKOFF games for partial prediction."""
    if not isinstance(eligible_game_ids, (list, tuple, set)) or not eligible_game_ids:
        raise ValueError("Partial lock is missing eligible game IDs")
    ids = [str(game_id) for game_id in eligible_game_ids]
    if any(not game_id or game_id.lower() == "nan" for game_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Partial lock eligible game IDs are empty or duplicated")
    eligible = set(ids)

    required_eligibility = {"game_id", "eligibility_state"}
    if not required_eligibility.issubset(eligibility.columns):
        raise ValueError(f"Partial eligibility lacks fields: {sorted(required_eligibility-set(eligibility.columns))}")
    if eligibility.game_id.astype(str).duplicated().any():
        raise ValueError("Partial eligibility contains duplicate game IDs")
    states = eligibility.set_index(eligibility.game_id.astype(str)).eligibility_state.astype(str)
    missing_state = eligible - set(states.index)
    if missing_state:
        raise ValueError(f"Eligible game IDs missing from eligibility state: {sorted(missing_state)}")
    non_pregame = sorted(game_id for game_id in eligible if states.loc[game_id] != "PRE_KICKOFF")
    if non_pregame:
        raise ValueError(f"Partial prediction IDs are not PRE_KICKOFF: {non_pregame}")
    blocked = set()
    if {"refresh_gate_status", "refresh_gate_reason"}.issubset(eligibility.columns):
        blocked = set(eligibility.loc[eligibility.refresh_gate_status.eq("BLOCKED") &
            eligibility.refresh_gate_reason.fillna("").astype(str).str.len().gt(0), "game_id"].astype(str))
    pregame_ids = set(states.index[states.eq("PRE_KICKOFF")]) - blocked
    if pregame_ids != eligible:
        raise ValueError("Partial lock eligible IDs do not match PRE_KICKOFF eligibility")
    if "eligible_for_refresh" in eligibility:
        refreshable = set(eligibility.loc[eligibility.eligible_for_refresh.astype(str).str.lower().isin(["true", "1"]), "game_id"].astype(str))
        if refreshable != eligible:
            raise ValueError("Partial lock eligible IDs do not match refreshable eligibility")

    if "game_id" not in context:
        raise ValueError("Partial team context has no game_id")
    context_ids = context.game_id.astype(str)
    counts = context_ids.value_counts()
    missing_context = eligible - set(counts.index)
    if missing_context:
        raise ValueError(f"Eligible games missing from partial team context: {sorted(missing_context)}")
    duplicate_context = sorted(game_id for game_id in eligible if int(counts.get(game_id, 0)) != 1)
    if duplicate_context:
        raise ValueError(f"Eligible games must occur exactly once in partial team context: {duplicate_context}")
    return context.loc[context_ids.isin(eligible)].copy().reset_index(drop=True)


def build_partial_current_team_matrix(
    context: pd.DataFrame,
    historical: pd.DataFrame,
    features: list[str],
    team_state: pd.DataFrame | None,
    target_week: int,
    eligible_game_ids,
    eligibility: pd.DataFrame,
    matrix_builder,
) -> pd.DataFrame:
    """Scope and validate games before invoking the strict production matrix builder."""
    eligible_context = select_partial_prediction_context(context, eligible_game_ids, eligibility)
    return matrix_builder(
        eligible_context, historical, features, team_state=team_state, target_week=target_week
    )


def assert_partial_team_matrix_coverage(current: pd.DataFrame, eligible_game_ids) -> None:
    """Defense-in-depth exact-ID coverage check after matrix construction."""
    if not isinstance(eligible_game_ids, (list, tuple, set)) or not eligible_game_ids:
        raise ValueError("Partial lock is missing eligible game IDs")
    expected = [str(game_id) for game_id in eligible_game_ids]
    if len(expected) != len(set(expected)):
        raise ValueError("Partial lock eligible game IDs are duplicated")
    if "game_id" not in current or current.game_id.astype(str).duplicated().any():
        raise ValueError("PIT team matrix has missing or duplicate game IDs")
    actual = set(current.game_id.astype(str))
    if actual != set(expected):
        raise ValueError("PIT team matrix does not cover exactly the eligible partial-week games")


def assert_partial_team_matrix_coverage(current: pd.DataFrame, eligible_game_ids) -> None:
    """Defense-in-depth exact-ID coverage check after matrix construction."""
    if not isinstance(eligible_game_ids, (list, tuple, set)) or not eligible_game_ids:
        raise ValueError("Partial lock is missing eligible game IDs")
    expected = [str(game_id) for game_id in eligible_game_ids]
    if len(expected) != len(set(expected)):
        raise ValueError("Partial lock eligible game IDs are duplicated")
    if "game_id" not in current or current.game_id.astype(str).duplicated().any():
        raise ValueError("PIT team matrix has missing or duplicate game IDs")
    actual = set(current.game_id.astype(str))
    if actual != set(expected):
        raise ValueError("PIT team matrix does not cover exactly the eligible partial-week games")


def build_partial_current_team_matrix(
    context: pd.DataFrame,
    historical: pd.DataFrame,
    features: list[str],
    team_state: pd.DataFrame | None,
    target_week: int,
    eligible_game_ids,
    eligibility: pd.DataFrame,
    matrix_builder,
) -> pd.DataFrame:
    """Scope and validate games before invoking the strict production matrix builder."""
    eligible_context = select_partial_prediction_context(context, eligible_game_ids, eligibility)
    return matrix_builder(
        eligible_context, historical, features, team_state=team_state, target_week=target_week
    )


def stage_unplayed_live_sources(settings, season: int, week: int, raw_dir: Path,
                                retrieved_at: datetime | None = None, weather_http_get=None):
    """Write a timestamped PRE_KICKOFF-only snapshot; shared weekly inputs remain untouched."""
    from nfl2026.live.pipeline import ASSETS

    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    raw_dir = Path(raw_dir)
    raw_schedule = pd.read_csv(raw_dir / "games.csv", low_memory=False)
    eligibility = classify_game_eligibility(raw_schedule, season, week, retrieved_at)
    eligible = set(eligibility.loc[eligibility.eligible_for_refresh, "game_id"].astype(str))
    if not eligible:
        raise ValueError("No PRE_KICKOFF games are eligible")
    schedule = _schedule_with_kickoff(raw_schedule, season, week)
    schedule = schedule[schedule.game_id.astype(str).isin(eligible)].copy()
    for col in ("away_score", "home_score", "result", "total", "overtime"):
        if col in schedule:
            schedule[col] = pd.NA
    teams = set(schedule.home_team) | set(schedule.away_team)
    roster_time = parse_nflverse_timestamp(raw_dir / "weekly_rosters_timestamp.json")
    roster, roster_status, roster_warnings = _roster_for_week(
        pd.read_csv(raw_dir / f"weekly_roster_{season}.csv", low_memory=False), season, week, teams)
    depth = load_depth(pd.read_csv(raw_dir / f"depth_charts_{season}.csv", low_memory=False), teams, retrieved_at, retrieved_at, season)
    roster["roster_original_team"] = roster.team
    market_cols=["away_moneyline","home_moneyline","spread_line","away_spread_odds","home_spread_odds","total_line","under_odds","over_odds"]
    schedule=schedule.drop(columns=[c for c in market_cols if c in schedule],errors="ignore")
    depth_team = depth.records.dropna(subset=["player_id"]).drop_duplicates("player_id").set_index("player_id").team.to_dict()
    resolved = roster.gsis_id.astype(str).map(depth_team)
    changed = resolved.notna() & resolved.ne(roster.team)
    roster.loc[changed, "team"] = resolved[changed]
    roster["team_reconciled_from_current_depth"] = changed
    raw_injuries = pd.read_csv(raw_dir / f"injuries_{season}.csv", low_memory=False)
    raw_injuries = raw_injuries[raw_injuries.team.isin(teams)].copy()
    injuries = load_injuries(raw_injuries, schedule, season, week,
        parse_nflverse_timestamp(raw_dir / "injuries_timestamp.json"), retrieved_at)
    official_path = raw_dir / "nfl_injuries.html"
    if official_path.exists():
        official = load_official_injury_tables(official_path.read_bytes(), roster, season, week, retrieved_at)
        injuries = merge_official_supplement(injuries, official, retrieved_at)
    from nfl2026.live.providers.club_availability import load_sources as load_availability
    from nfl2026.live.availability import apply_current_sources
    availability_events,availability_coverage=load_availability(raw_dir,roster,schedule,retrieved_at)
    injuries=apply_current_sources(injuries,availability_events,availability_coverage,schedule,retrieved_at)
    from nfl2026.live.providers.club_transactions import load_sources as load_club_transactions
    from nfl2026.live.providers.transactions import overlay_transactions_on_roster
    retrieval_meta=json.loads((raw_dir/'retrieval.json').read_text()) if (raw_dir/'retrieval.json').exists() else {}
    pages={p.name:p.read_bytes() for p in raw_dir.glob('nfl_transactions_*_page*.html')}
    categories={r['category'] for r in retrieval_meta.get('official_transaction_categories',[]) if r.get('complete')}
    transactions = load_transactions(pd.read_csv(raw_dir / "trades.csv", low_memory=False), season, retrieved_at,
        parse_nflverse_timestamp(raw_dir / "trades_timestamp.json"), retrieved_at,pages,categories,
        load_club_transactions(raw_dir,season,retrieved_at))
    roster,_ = overlay_transactions_on_roster(roster,transactions.records,raw_schedule,
        cutoff=retrieved_at,roster_known_at=roster_time)
    evidence = load_evidence(raw_dir, roster, raw_schedule, retrieved_at)
    depth = load_depth(pd.read_csv(raw_dir/f'depth_charts_{season}.csv',low_memory=False),teams,retrieved_at,retrieved_at,season,
        roster=roster,injuries=injuries.records,transactions=transactions.records,transaction_coverage=transactions.coverage.assign(known_at_utc=transactions.source_known_at_utc,retrieved_at_utc=transactions.retrieved_at_utc))
    schedule = reconcile_quarterbacks(schedule, roster, depth.records, injuries.records,
                                     week, retrieved_at, evidence)
    qbs = load_quarterbacks(schedule, roster, depth.records, week, retrieved_at, injuries.records)
    if len(transactions.records) and {"from_team", "to_team"}.issubset(transactions.records.columns):
        transactions.records = transactions.records[transactions.records.from_team.isin(teams) | transactions.records.to_team.isin(teams)].copy()
        transactions.coverage["records_pre_cutoff"] = len(transactions.records)
        transactions.source_known_at_basis += "; rows filtered to teams with PRE_KICKOFF games"
    stadiums = pd.read_csv(settings.legacy_root / "2025 Stats" / "stadiums_2025.csv")
    weather = load_weather(schedule, stadiums, retrieved_at, weather_http_get)
    team_game = {team: str(g.game_id) for g in schedule.itertuples(index=False) for team in (g.home_team, g.away_team)}
    for frame in (roster, depth.records, injuries.records):
        if "team" in frame:
            frame["game_id"] = frame.team.astype(str).map(team_game)
    schedule["eligibility_state"] = "PRE_KICKOFF"
    schedule["refresh_timestamp_utc"] = utc_iso(retrieved_at)
    stamp = retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    refresh_id = f"refresh-{stamp}-{hashlib.sha256('|'.join(sorted(eligible)).encode()).hexdigest()[:8]}"
    dest = settings.root / "data" / "live_refresh" / str(season) / f"week_{week:02d}" / refresh_id
    if dest.exists():
        raise FileExistsError(f"Game-keyed refresh is immutable: {dest}")
    dest.mkdir(parents=True)
    eligibility.to_csv(dest / "game_eligibility.csv", index=False, lineterminator="\n")
    schedule.to_csv(dest / "games.csv", index=False, lineterminator="\n")
    files = {"roster": f"weekly_roster_{season}.csv", "injuries": f"injuries_{season}.csv", "depth": f"depth_charts_{season}.csv",
        "quarterbacks": f"qb_status_{season}_w{week:02d}.csv", "transactions": f"transactions_{season}.csv", "weather": f"weather_{season}_w{week:02d}.csv"}
    frames = {"roster": roster, "injuries": injuries.records, "depth": depth.records, "quarterbacks": qbs.records,
        "transactions": transactions.records, "weather": weather.records}
    covers = {"injuries": injuries.coverage, "depth": depth.coverage, "quarterbacks": qbs.coverage,
        "transactions": transactions.coverage, "weather": weather.coverage}
    results = {r.name: r for r in (injuries, depth, qbs, transactions, weather)}
    provenance = []
    for name, frame in frames.items():
        path = dest / files[name]
        frame.to_csv(path, index=False, lineterminator="\n")
        coverage = covers.get(name, pd.DataFrame())
        if len(coverage):
            coverage.to_csv(dest / f"{name}_coverage_{season}_w{week:02d}.csv", index=False, lineterminator="\n")
        for game in eligibility[eligibility.eligible_for_refresh].itertuples(index=False):
            state = "VALID"
            if name == "injuries":
                rows = coverage[coverage.team.isin([game.home_team, game.away_team])]
                states = set(rows.source_state.astype(str)) if "source_state" in rows else set()
                state = next((x for x in ("ERROR", "STALE", "SOURCE_MISSING", "NOT_YET_PUBLISHED") if x in states), "VALID")
            elif name == "quarterbacks":
                rows = coverage[coverage.game_id.astype(str).eq(str(game.game_id))]
                states = set(rows.source_state.astype(str)) if "source_state" in rows else set()
                state = next((x for x in ("ERROR", "STALE", "SOURCE_MISSING") if x in states), "VALID")
            elif name == "depth":
                rows = coverage[coverage.team.isin([game.home_team, game.away_team])]
                state = "VALID" if len(rows) == 2 and rows.coverage_status.eq("EXACT_CURRENT_DEPTH").all() else "SOURCE_MISSING"
            elif name == "weather":
                rows = coverage[coverage.game_id.astype(str).eq(str(game.game_id))]
                state = "VALID" if len(rows) and rows.forecast_status.iloc[0] in {"AVAILABLE", "ROOF_SUPPRESSED"} else "ERROR"
            result = results.get(name)
            provenance.append({"game_id": str(game.game_id), "season": season, "week": week, "kickoff_utc": game.kickoff_utc,
                "refresh_timestamp_utc": utc_iso(retrieved_at), "provider": name, "source_state": state,
                "eligibility_state": "PRE_KICKOFF", "artifact": path.name, "artifact_sha256": sha256_file(path),
                "source_url": result.source_url if result else ASSETS["weekly_roster_2026.csv"].replace("2026", str(season)),
                "source_known_at_utc": result.source_known_at_utc if result else utc_iso(roster_time)})
    pd.DataFrame(provenance).to_csv(dest / "game_refresh_provenance.csv", index=False, lineterminator="\n")
    providers = _provider_manifest([injuries, depth, qbs, transactions, weather], season, week)
    providers.to_csv(dest / f"live_provider_manifest_{season}_w{week:02d}.csv", index=False, lineterminator="\n")
    manifest = {"refresh_id": refresh_id, "season": season, "week": week, "refresh_timestamp_utc": utc_iso(retrieved_at),
        "eligible_game_ids": sorted(eligible), "eligibility_counts": eligibility.eligibility_state.value_counts().to_dict(),
        "source_files": {p.name: sha256_file(p) for p in dest.glob("*.csv")},
        "scope": "PRE_KICKOFF games only; no result/stat/PBP inputs", "roster_status": roster_status, "roster_warnings": roster_warnings}
    (dest / "game_refresh_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    return dest, eligibility


def _registered_partial_player_contract(settings):
    """Load the registered fitted player/TD contracts used for partial-lock validation."""
    import joblib
    from sklearn.impute import SimpleImputer
    from nfl2026.registry.core import load_registered_model

    registry = settings.artifact_root / "registry" / "model_registry.csv"
    player_path, player_meta = load_registered_model(
        registry, "player_opportunity_model", settings.phase2_player_model_version,
        settings.phase2_feature_version)
    td_path, td_meta = load_registered_model(
        registry, "player_td_model", settings.phase2_model_version,
        settings.phase2_feature_version)
    player_payload = joblib.load(player_path)
    td_payload = joblib.load(td_path)
    player_features = list(player_payload.get("features", []))
    td_features = list(td_payload.get("features", []))
    if not player_features or player_features != td_features:
        raise ValueError("Registered player and TD model feature contracts differ")

    def registry_features(metadata):
        raw = metadata.get("feature_list", "[]")
        try:
            return json.loads(raw) if isinstance(raw, str) else list(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Registered model feature list is invalid") from exc

    if registry_features(player_meta) != player_features or registry_features(td_meta) != td_features:
        raise ValueError("Registered artifact inputs do not match registry feature contracts")
    player_models = player_payload.get("models")
    td_model = td_payload.get("model")
    if not isinstance(player_models, dict) or set(player_models) != {
            "carries", "targets", "rushing_yards", "receiving_yards"} or td_model is None:
        raise ValueError("Registered player/TD artifact has an unexpected model structure")
    pipelines = [(f"player_opportunity:{name}", model) for name, model in player_models.items()]
    pipelines.append(("player_td", td_model))
    for label, pipeline in pipelines:
        steps = getattr(pipeline, "steps", None)
        if not steps:
            raise ValueError(f"{label} is not a fitted production pipeline")
        imputers = [(index, step) for index, (name, step) in enumerate(steps)
                    if isinstance(step, SimpleImputer)]
        if not imputers:
            raise ValueError(f"{label} has no fitted SimpleImputer")
        imputer_index, imputer = imputers[0]
        if imputer_index >= len(steps) - 1:
            raise ValueError(f"{label} imputer is not before its fitted estimator")
        if len(getattr(imputer, "statistics_", ())) != len(player_features):
            raise ValueError(f"{label} imputer input width differs from the registered contract")
        fitted_names = getattr(imputer, "feature_names_in_", None)
        if fitted_names is not None and list(map(str, fitted_names)) != player_features:
            raise ValueError(f"{label} fitted imputer column order differs from the registered contract")
    return {
        "features": player_features,
        "pipelines": pipelines,
        "pipeline_steps": {label: [name for name, _ in pipeline.steps]
                           for label, pipeline in pipelines},
        "player_artifact": str(player_path.relative_to(settings.root)),
        "player_artifact_sha256": sha256_file(player_path),
        "td_artifact": str(td_path.relative_to(settings.root)),
        "td_artifact_sha256": sha256_file(td_path),
    }


def _validate_partial_player_input_contract(candidates: pd.DataFrame, contract: dict) -> dict:
    """Prove schema and fitted missing-value handling without predicting outcomes."""
    import numpy as np

    features = list(contract["features"])
    absent = sorted(set(features) - set(candidates.columns))
    if absent:
        raise ValueError(f"Partial player feature schema lacks registered model inputs: {absent}")
    forbidden = [column for column in candidates.columns
                 if str(column).lower().startswith(("actual_", "target_", "pbp_"))
                 or str(column).lower() in {"home_score", "away_score", "final_score", "postgame_result"}]
    if forbidden:
        raise ValueError(f"Partial player matrix contains outcome/PBP columns: {sorted(forbidden)}")

    missing_features = [feature for feature in features if candidates[feature].isna().any()]
    for label, pipeline in contract["pipelines"]:
        steps = pipeline.steps
        imputer = next((step for _, step in steps if type(step).__name__ == "SimpleImputer"), None)
        if imputer is None:
            raise ValueError(f"{label} cannot safely consume missing player feature values")
        stats = np.asarray(imputer.statistics_, dtype=float)
        for feature in missing_features:
            value = stats[features.index(feature)]
            if not np.isfinite(value):
                raise ValueError(f"{label} has no trained imputation value for {feature}")
        # Exercise the fitted transformer only; do not call estimator.predict.
        transformed = imputer.transform(candidates.loc[:, features])
        if transformed.shape != (len(candidates), len(features)) or not np.isfinite(transformed).all():
            raise ValueError(f"{label} fitted imputer does not preserve the registered input schema")
    return {"features": features, "missing_model_features": missing_features}


def _resolve_frozen_team_feature_source(settings, season: int, week: int, cutoff: datetime) -> dict | None:
    """Resolve the latest valid full-week run's immutable team state, without rebuilding it."""
    if week < 4:
        return None
    from nfl2026.frontend.build import select_official_bundle
    from nfl2026.production.core import validate_bundle, verify_lock

    bundle = select_official_bundle(settings, season, week, pd.Timestamp(cutoff))
    bundle_meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    if bundle_meta.get("prediction_scope", "FULL_WEEK") != "FULL_WEEK":
        raise ValueError("Team feature inheritance requires a full-week official bundle")
    validation = validate_bundle(bundle)
    if validation.empty or not validation.passed.astype(bool).all():
        raise ValueError("Selected official bundle failed immutable artifact validation")
    lock_id = str(bundle_meta.get("lock_id", ""))
    if not lock_id:
        raise ValueError("Selected official bundle has no source lock ID")
    source_lock = settings.root / "locks" / str(season) / f"week_{week:02d}" / lock_id
    if not source_lock.is_dir():
        raise FileNotFoundError(f"Selected official bundle lock is missing: {lock_id}")
    lock_errors = verify_lock(source_lock)
    if lock_errors:
        raise ValueError(f"Selected official bundle lock failed verification: {lock_errors}")
    lock_meta = json.loads((source_lock / "data_lock.json").read_text(encoding="utf-8"))
    if lock_meta.get("lock_status") == "BLOCKED" or lock_meta.get("production_blockers"):
        raise ValueError("Selected official bundle references a blocked source lock")
    embedded_lock_path = bundle / "data_lock.json"
    if not embedded_lock_path.exists():
        raise FileNotFoundError("Selected official bundle lacks its embedded data lock")
    embedded_lock = json.loads(embedded_lock_path.read_text(encoding="utf-8"))
    if (int(lock_meta.get("season", -1)) != season or int(lock_meta.get("week", -1)) != week
            or lock_meta.get("lock_id") != lock_id
            or lock_meta.get("team_feature_state_sha256") is None
            or lock_meta.get("team_feature_manifest_sha256") is None
            or embedded_lock.get("lock_id") != lock_id
            or embedded_lock.get("team_feature_state_sha256") != lock_meta.get("team_feature_state_sha256")
            or embedded_lock.get("team_feature_manifest_sha256") != lock_meta.get("team_feature_manifest_sha256")):
        raise ValueError("Selected source lock does not satisfy the Week 4+ team feature contract")
    state_name = f"team_feature_state_{season}_w{week:02d}.csv"
    manifest_name = f"team_feature_state_{season}_w{week:02d}.json"
    state_path = source_lock / "inputs" / state_name
    manifest_path = source_lock / "inputs" / manifest_name
    if not state_path.exists() or not manifest_path.exists():
        raise FileNotFoundError("Selected source lock lacks immutable team feature state files")
    state_hash = sha256_file(state_path)
    manifest_hash = sha256_file(manifest_path)
    if state_hash != lock_meta["team_feature_state_sha256"] or manifest_hash != lock_meta["team_feature_manifest_sha256"]:
        raise ValueError("Selected source lock's team feature state hashes do not verify")
    state_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    generated_at = pd.to_datetime(state_meta.get("generated_at_utc"), utc=True, errors="coerce")
    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None or pd.isna(generated_at) or generated_at > cutoff_ts:
        raise ValueError("Selected team feature state is not point-in-time safe for this partial lock")
    if (int(state_meta.get("season", -1)) != season or int(state_meta.get("target_week", -1)) != week
            or int(state_meta.get("latest_completed_source_week", week)) >= week
            or state_meta.get("state_file_sha256") != state_hash
            or state_meta.get("feature_version") != settings.phase2_feature_version):
        raise ValueError("Selected team feature state manifest is incompatible or includes target-week data")
    return {
        "bundle": bundle,
        "bundle_meta": bundle_meta,
        "lock": source_lock,
        "lock_meta": lock_meta,
        "state_path": state_path,
        "manifest_path": manifest_path,
        "state_name": state_name,
        "manifest_name": manifest_name,
        "state_sha256": state_hash,
        "manifest_sha256": manifest_hash,
        "generated_at_utc": generated_at.isoformat(),
        "run_id": str(bundle_meta.get("run_id", bundle.name)),
        "lock_id": lock_id,
    }


def _copy_frozen_team_feature_source(source: dict | None, inputs: Path, root: Path, season: int, week: int) -> tuple[dict, list[dict]]:
    if source is None:
        return {}, []
    state_target = inputs / source["state_name"]
    manifest_target = inputs / source["manifest_name"]
    shutil.copy2(source["state_path"], state_target)
    shutil.copy2(source["manifest_path"], manifest_target)
    if sha256_file(state_target) != source["state_sha256"] or sha256_file(manifest_target) != source["manifest_sha256"]:
        raise ValueError("Inherited team feature state changed while copying into partial lock")
    state_rel = str(state_target.relative_to(root))
    manifest_rel = str(manifest_target.relative_to(root))
    common = {
        "source_state": "VALID", "retrieved_at_utc": source["generated_at_utc"],
        "source_known_at_utc": source["generated_at_utc"], "known_at_utc": source["generated_at_utc"],
        "valid_from": source["generated_at_utc"], "season": season, "week": week,
        "data_status": "READY", "freshness_status": "READY", "critical": True,
        "selected_for_lock": True,
    }
    state_row = {**common, "source": "team_feature_state", "source_url": f"immutable lock {source['lock_id']} from official run {source['run_id']}",
        "snapshot_path": state_rel, "locked_path": state_rel, "row_count": len(pd.read_csv(state_target)),
        "sha256": source["state_sha256"], "schema_version": "csv", "reliability": "INHERITED_VALIDATED_PIT_TEAM_FEATURE_STATE",
        "quality_warnings": "Copied byte-identically; no feature recalculation"}
    manifest_row = {**common, "source": "team_feature_state_manifest", "source_url": f"immutable lock {source['lock_id']} from official run {source['run_id']}",
        "snapshot_path": manifest_rel, "locked_path": manifest_rel, "row_count": 1,
        "sha256": source["manifest_sha256"], "schema_version": "json-v1", "reliability": "INHERITED_TEAM_FEATURE_PROVENANCE",
        "quality_warnings": "Original source manifest copied byte-identically"}
    metadata = {
        "team_feature_state_sha256": source["state_sha256"],
        "team_feature_manifest_sha256": source["manifest_sha256"],
        "team_feature_state_source_policy": "LATEST_VALID_OFFICIAL_FULL_WEEK_RUN_AT_OR_BEFORE_CUTOFF",
        "team_feature_state_source_run_id": source["run_id"],
        "team_feature_state_source_lock_id": source["lock_id"],
        "team_feature_state_source_run_created_at_utc": source["bundle_meta"].get("created_at_utc", ""),
        "team_feature_state_source_lock_manifest_sha256": source["lock_meta"].get("manifest_sha256", ""),
    }
    return metadata, [state_row, manifest_row]

def create_partial_week_lock(settings, season: int, week: int, cutoff: datetime):
    """Lock the latest verified game-keyed refresh for its PRE_KICKOFF games only."""
    root = settings.root / "data" / "live_refresh" / str(season) / f"week_{week:02d}"
    choices = sorted(root.glob("refresh-*/game_refresh_manifest.json")) if root.exists() else []
    if not choices:
        raise FileNotFoundError(f"No game-keyed live refresh for {season} Week {week}")
    manifest_path = choices[-1]
    refresh = manifest_path.parent
    meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    retrieved = pd.Timestamp(meta["refresh_timestamp_utc"])
    cutoff_ts = pd.Timestamp(cutoff)
    if cutoff_ts.tzinfo is None or retrieved > cutoff_ts:
        raise ValueError("Partial refresh is post-cutoff or cutoff is not timezone-aware")
    for name, digest in meta["source_files"].items():
        path = refresh / name
        if not path.exists() or sha256_file(path) != digest:
            raise ValueError(f"Partial refresh artifact hash mismatch: {name}")
    eligibility = pd.read_csv(refresh / "game_eligibility.csv")
    if eligibility.game_id.astype(str).duplicated().any():
        raise ValueError("Partial refresh eligibility has duplicate game IDs")
    states = set(eligibility.eligibility_state.astype(str))
    if not states.issubset({"PRE_KICKOFF", "LIVE", "FINAL", "AMBIGUOUS"}):
        raise ValueError(f"Unsupported game state in partial refresh: {sorted(states)}")
    eligible = set(eligibility.loc[eligibility.eligible_for_refresh.astype(bool), "game_id"].astype(str))
    if not eligible:
        raise ValueError("Partial-week production requires at least one PRE_KICKOFF game")
    if set(eligibility.loc[eligibility.eligibility_state.eq("PRE_KICKOFF"), "game_id"].astype(str)) != eligible:
        raise ValueError("PRE_KICKOFF states and eligible game IDs disagree")
    if eligible != set(map(str, meta["eligible_game_ids"])):
        raise ValueError("Eligibility manifest and refresh scope differ")
    games = pd.read_csv(refresh / "games.csv", low_memory=False)
    if set(games.game_id.astype(str)) != eligible or games.game_id.duplicated().any():
        raise ValueError("Partial schedule contains games outside its eligible scope")
    if games[["away_score", "home_score"]].notna().any().any():
        raise ValueError("Partial schedule contains outcome scores")
    kickoffs = pd.to_datetime(games.kickoff_utc, utc=True, errors="coerce")
    if kickoffs.isna().any():
        raise ValueError("Partial schedule contains an ambiguous kickoff")
    # A refresh may have been captured before a fixture started. Archive only
    # the still-future subset; elapsed fixtures retain their original forecast.
    elapsed_ids=set(games.loc[kickoffs.le(cutoff_ts),"game_id"].astype(str))
    if elapsed_ids:
        eligibility.loc[eligibility.game_id.astype(str).isin(elapsed_ids),"eligibility_state"]="LIVE"
        eligibility.loc[eligibility.game_id.astype(str).isin(elapsed_ids),"eligible_for_refresh"]=False
        eligible-=elapsed_ids
        games=games[games.game_id.astype(str).isin(eligible)].copy()
        if not eligible:
            raise ValueError("Partial-week production requires at least one PRE_KICKOFF game; all originals retained")
    from nfl2026.production.evaluation import select_evaluation_runs
    existing_predictions = select_evaluation_runs(settings, season, week, cutoff)
    immutable_ids = set(eligibility.loc[eligibility.eligibility_state.isin(["FINAL", "LIVE"]), "game_id"].astype(str))
    missing_immutable = immutable_ids - set(existing_predictions.game_id.astype(str))
    if missing_immutable:
        raise FileNotFoundError(f"Missing frozen predictions for immutable games: {sorted(missing_immutable)}")
    # Validate the global PIT feature source before creating a destination. Only
    # its game-scoped copy may receive missing legacy provenance metadata.
    player_source = settings.artifact_root / "phase2" / "player_pregame_universe_features.csv"
    if not player_source.exists():
        raise FileNotFoundError("Partial run requires the existing pregame player feature matrix")
    player_source_sha256 = sha256_file(player_source)
    player_features = pd.read_csv(player_source, low_memory=False)
    required_player = {"game_id", "season", "week", "player_id", "team", "position",
                       "status_eligible", "feature_version", "prediction_cutoff_utc"}
    if not required_player.issubset(player_features.columns):
        raise ValueError(f"Partial player feature matrix lacks PIT fields: {sorted(required_player-set(player_features.columns))}")
    candidates = player_features[(player_features.season.eq(season)) & player_features.week.eq(week)
        & player_features.game_id.astype(str).isin(eligible)].copy()
    if candidates.empty or candidates.game_id.astype(str).nunique() != len(eligible):
        raise ValueError("PIT player feature matrix does not cover every eligible game")
    candidates = candidates[candidates.status_eligible.astype(str).str.lower().isin(["true", "1"])].copy()
    if candidates.empty or candidates.duplicated(["game_id", "player_id"]).any():
        raise ValueError("PIT player feature rows are empty or duplicated")
    versions = candidates.feature_version
    native_mask = versions.notna() & versions.astype("string").eq(settings.phase2_feature_version)
    missing_mask = versions.isna()
    wrong_mask = versions.notna() & ~versions.astype("string").eq(settings.phase2_feature_version)
    if wrong_mask.any():
        wrong = sorted(versions[wrong_mask].astype(str).unique())
        raise ValueError(f"Partial player feature version does not match production configuration: {wrong}")

    target_kickoffs = pd.to_datetime(eligibility.kickoff_utc, utc=True, errors="coerce")
    week_pit_ceiling = target_kickoffs.min()
    feature_cutoff = pd.to_datetime(candidates.prediction_cutoff_utc, utc=True, errors="coerce")
    if feature_cutoff.isna().any() or feature_cutoff.ge(week_pit_ceiling).any():
        raise ValueError("Player features are not demonstrably frozen before the first target-week kickoff")
    if "effective_utc" in candidates:
        effective = pd.to_datetime(candidates.effective_utc, utc=True, errors="coerce")
        # An audited coverage-limited row may have no player-specific event
        # timestamp. The complete matrix cutoff must still predate the week.
        if effective.dropna().ge(week_pit_ceiling).any():
            raise ValueError("Player feature provenance includes target-week information")

    source_schema_sha256 = hashlib.sha256(json.dumps(
        list(map(str, player_features.columns)), separators=(",", ":")).encode("utf-8")).hexdigest()
    model_contract = None
    model_input_audit = {"features": [], "missing_model_features": []}
    if missing_mask.any():
        model_contract = _registered_partial_player_contract(settings)
        model_input_audit = _validate_partial_player_input_contract(candidates, model_contract)
        # Provenance omission is repairable only where the same target-week
        # matrix has native rows for the configured version and the fitted
        # production contracts accept the row's missing inputs.
        if not native_mask.any():
            raise ValueError("Missing legacy feature version cannot be normalized without native configured-version peers")
        normalized_version = settings.phase2_feature_version
        candidates.loc[missing_mask, "feature_version"] = normalized_version
    else:
        model_input_audit = {"features": [], "missing_model_features": []}

    lock_id = f"lock-{season}-w{week:02d}-partial-{meta['refresh_id'].removeprefix('refresh-')}"
    if elapsed_ids:
        lock_id += "-remaining-"+hashlib.sha256(",".join(sorted(eligible)).encode()).hexdigest()[:8]
    if missing_mask.any():
        # Give the metadata-compatible lock a distinct immutable identity; the
        # earlier failed/incomplete lock remains untouched for audit history.
        lock_id += f"-legacy-meta-{player_source_sha256[:8]}"
    team_feature_source = _resolve_frozen_team_feature_source(settings, season, week, cutoff)
    destination = settings.root / "locks" / str(season) / f"week_{week:02d}" / lock_id
    if destination.exists():
        existing_meta_path = destination / "data_lock.json"
        existing_manifest_path = destination / "lock_manifest.csv"
        if existing_meta_path.exists() and existing_manifest_path.exists():
            existing_meta = json.loads(existing_meta_path.read_text(encoding="utf-8"))
            same_inputs = (
                existing_meta.get("lock_id") == lock_id
                and existing_meta.get("snapshot_id") == meta.get("refresh_id")
                and existing_meta.get("feature_version") == settings.phase2_feature_version
                and existing_meta.get("source_player_feature_artifact_sha256") == player_source_sha256
                and existing_meta.get("game_refresh_manifest_sha256") == sha256_file(manifest_path)
                and existing_meta.get("manifest_sha256") == sha256_file(existing_manifest_path)
                and int(existing_meta.get("legacy_metadata_normalizations", -1)) == int(missing_mask.sum())
                and existing_meta.get("eligible_game_ids") == sorted(eligible)
                and (team_feature_source is None or (
                    existing_meta.get("team_feature_state_source_run_id") == team_feature_source["run_id"]
                    and existing_meta.get("team_feature_state_source_lock_id") == team_feature_source["lock_id"]
                    and existing_meta.get("team_feature_state_sha256") == team_feature_source["state_sha256"]
                    and existing_meta.get("team_feature_manifest_sha256") == team_feature_source["manifest_sha256"]
                ))
            )
            if same_inputs:
                from nfl2026.production.core import verify_lock
                lock_errors = verify_lock(destination)
                if not lock_errors:
                    return destination, pd.read_csv(existing_manifest_path), existing_meta
                raise ValueError(f"Existing immutable partial lock failed verification: {lock_errors}")
        raise FileExistsError(f"Partial lock is immutable: {destination}")
    inputs = destination / "inputs"
    inputs.mkdir(parents=True)
    inherited_team_meta, inherited_team_rows = _copy_frozen_team_feature_source(
        team_feature_source, inputs, settings.root, season, week
    )
    mapping = {"schedule": "games.csv", "roster": f"weekly_roster_{season}.csv", "injuries": f"injuries_{season}.csv",
        "depth": f"depth_charts_{season}.csv", "quarterbacks": f"qb_status_{season}_w{week:02d}.csv",
        "transactions": f"transactions_{season}.csv", "weather": f"weather_{season}_w{week:02d}.csv"}
    provenance = pd.read_csv(refresh / "game_refresh_provenance.csv")
    rows = list(inherited_team_rows)
    for source, filename in mapping.items():
        source_path = refresh / filename
        if not source_path.exists():
            continue
        frame = pd.read_csv(source_path, low_memory=False)
        known = pd.to_datetime(frame.get("known_at_utc", frame.get("source_known_at_utc", pd.Series(dtype=object))), utc=True, errors="coerce")
        if known.notna().any() and known.gt(cutoff_ts).any():
            raise ValueError(f"Post-cutoff source row in {filename}")
        target = inputs / filename
        if source=="schedule" and elapsed_ids:
            games.to_csv(target,index=False,lineterminator="\n")
        else:
            shutil.copy2(source_path, target)
        per_game = provenance[provenance.provider.eq(source)] if source != "schedule" else provenance.iloc[0:0]
        source_states = set(per_game.source_state.astype(str)) if len(per_game) else {"VALID"}
        state = next((s for s in ("ERROR", "STALE", "SOURCE_MISSING") if s in source_states),
                     "NOT_YET_PUBLISHED" if "NOT_YET_PUBLISHED" in source_states else "VALID")
        critical = source in {"schedule", "roster", "injuries", "quarterbacks"}
        status = "BLOCKED" if critical and state in {"ERROR", "STALE", "SOURCE_MISSING"} else "READY_WITH_WARNINGS" if state != "VALID" else "READY"
        rows.append({"source": source, "source_url": per_game.source_url.iloc[0] if len(per_game) else "nflverse current schedule",
            "source_state": state, "retrieved_at_utc": meta["refresh_timestamp_utc"],
            "known_at_utc": per_game.source_known_at_utc.min() if len(per_game) else meta["refresh_timestamp_utc"],
            "season": season, "week": week, "snapshot_path": str(target.relative_to(settings.root)),
            "sha256": sha256_file(target), "row_count": len(frame), "data_status": status,
            "freshness_status": status, "critical": critical, "reliability": "GAME_KEYED_PREGAME_REFRESH",
            "quality_warnings": "" if state == "VALID" else f"source state {state}"})

    candidate_path = inputs / "player_pregame_universe_features.csv"
    candidates.to_csv(candidate_path, index=False, lineterminator="\n")
    normalization_timestamp = datetime.now(timezone.utc).isoformat()
    normalization_rows = []
    for row in candidates.loc[missing_mask].itertuples(index=False):
        normalization_rows.append({
            "game_id": str(row.game_id), "player_id": str(row.player_id),
            "player_name": str(getattr(row, "player_name", "")),
            "source_artifact": str(player_source.relative_to(settings.root)),
            "source_artifact_sha256": player_source_sha256,
            "source_feature_version": "MISSING",
            "normalized_feature_version": settings.phase2_feature_version,
            "reason": "LEGACY_METADATA_NORMALIZED: registered Phase 2 input schema and fitted missing-value pipeline compatible; source cutoff predates target week",
            "compatibility_check_result": "PASS",
            "prediction_cutoff_utc": str(row.prediction_cutoff_utc),
            "schema_sha256": source_schema_sha256,
            "normalization_timestamp_utc": normalization_timestamp,
        })
    normalization_path = inputs / "partial_feature_normalization_audit.csv"
    pd.DataFrame(normalization_rows, columns=["game_id", "player_id", "player_name", "source_artifact",
        "source_artifact_sha256", "source_feature_version", "normalized_feature_version", "reason",
        "compatibility_check_result", "prediction_cutoff_utc", "schema_sha256",
        "normalization_timestamp_utc"]).to_csv(normalization_path, index=False, lineterminator="\n")
    rows.append({"source": "pregame_player_features", "source_url": "locked production PIT feature matrix",
        "source_state": "VALID", "retrieved_at_utc": meta["refresh_timestamp_utc"],
        "known_at_utc": feature_cutoff.max().isoformat(), "season": season, "week": week,
        "snapshot_path": str(candidate_path.relative_to(settings.root)), "sha256": sha256_file(candidate_path),
        "row_count": len(candidates), "data_status": "READY", "freshness_status": "READY", "critical": True,
        "reliability": "PREGAME_FEATURE_VERSION_MATCH; target-week rows scoped to eligible games" +
            ("; LEGACY_METADATA_NORMALIZED" if missing_mask.any() else ""),
        "quality_warnings": (f"Effective timestamps verified before first target-week kickoff; "
            f"{int(missing_mask.sum())} missing-version legacy row(s) normalized in this lock only")
            if missing_mask.any() else "Effective timestamps verified before first target-week kickoff"})
    if missing_mask.any():
        rows.append({"source": "partial_feature_normalization_audit", "source_url": "game-scoped compatibility audit",
            "source_state": "VALID", "retrieved_at_utc": normalization_timestamp,
            "known_at_utc": normalization_timestamp, "season": season, "week": week,
            "snapshot_path": str(normalization_path.relative_to(settings.root)),
            "sha256": sha256_file(normalization_path), "row_count": len(normalization_rows),
            "data_status": "READY", "freshness_status": "READY", "critical": True,
            "reliability": "LEGACY_METADATA_NORMALIZATION_PROVENANCE",
            "quality_warnings": "No feature values were modified"})

    # `_current_team_matrix` uses prior completed schedule games for its Elo
    # state. Lock a schedule context with every target/future week outcome
    # removed, and overlay only the eligible game's current pregame row.
    shared_schedule = settings.root / "data" / "phase2_external" / "games.csv"
    if not shared_schedule.exists():
        raise FileNotFoundError("Partial run requires the schedule context for pre-target-week state")
    context = pd.read_csv(shared_schedule, low_memory=False)
    context = context[context.game_type.astype(str).str.upper().eq("REG")].copy()
    if context.game_id.astype(str).duplicated().any():
        raise ValueError("PIT schedule context has duplicate game IDs")
    market_tokens = ("moneyline", "spread_line", "spread_odds", "total_line", "over_odds", "under_odds")
    context = context.drop(columns=[column for column in context.columns if any(token in column.lower() for token in market_tokens)], errors="ignore")
    all_target_ids = set(eligibility.game_id.astype(str))
    week_schedule = context[(context.season.eq(season)) & context.week.eq(week)]
    if set(week_schedule.game_id.astype(str)) != all_target_ids:
        raise ValueError("PIT schedule context does not match complete target-week schedule")
    # Match the full production context contract using canonical scheduled
    # local date/time.  Only target-week kickoffs are required here; historical
    # rows remain available for Elo ordering but are not guessed if malformed.
    context = normalize_schedule_kickoffs(context, required_game_ids=all_target_ids)
    outcome_columns = [c for c in ("away_score", "home_score", "result", "total", "overtime") if c in context]
    context.loc[context.season.eq(season) & context.week.ge(week), outcome_columns] = pd.NA
    live_games = games.set_index("game_id", drop=False)
    context = context.set_index("game_id", drop=False)
    for game_id in eligible:
        if game_id not in context.index:
            raise ValueError(f"Eligible game {game_id} missing from PIT schedule context")
        for column in games.columns:
            if column in context.columns and column not in outcome_columns:
                context.loc[game_id, column] = live_games.loc[game_id, column]
    target_context = context.loc[context.season.eq(season) & context.week.eq(week)]
    target_context_kickoffs = pd.to_datetime(target_context.kickoff_utc, utc=True, errors="coerce")
    if target_context_kickoffs.isna().any():
        missing_games = target_context.loc[target_context_kickoffs.isna(), "game_id"].astype(str).tolist()
        raise ValueError(f"PIT target-week context has unresolved scheduled kickoffs: {sorted(missing_games)}")
    context_path = inputs / "team_context_schedule.csv"
    context.reset_index(drop=True).to_csv(context_path, index=False, lineterminator="\n")
    rows.append({"source": "pit_team_schedule_context", "source_url": "sanitized current schedule; target/future results removed",
        "source_state": "VALID", "retrieved_at_utc": meta["refresh_timestamp_utc"],
        "known_at_utc": meta["refresh_timestamp_utc"], "season": season, "week": week,
        "snapshot_path": str(context_path.relative_to(settings.root)), "sha256": sha256_file(context_path),
        "row_count": len(context), "data_status": "READY", "freshness_status": "READY", "critical": True,
        "reliability": "TARGET_WEEK_OUTCOMES_SCRUBBED; prior-week outcomes retained",
        "quality_warnings": "Week W and future results are excluded"})

    historical_source = settings.artifact_root / "phase2" / "team_advanced_matrix.csv"
    if not historical_source.exists():
        raise FileNotFoundError("Partial run requires registered-model historical team features")
    historical = pd.read_csv(historical_source, low_memory=False)
    if int(pd.to_numeric(historical.season, errors="coerce").max()) >= season:
        raise ValueError("Historical team feature matrix contains current/future season rows")
    historical_path = inputs / "historical_team_features.csv"
    shutil.copy2(historical_source, historical_path)
    rows.append({"source": "historical_team_features", "source_url": "registered-model historical PIT feature matrix",
        "source_state": "VALID", "retrieved_at_utc": meta["refresh_timestamp_utc"],
        "known_at_utc": meta["refresh_timestamp_utc"], "season": season, "week": week,
        "snapshot_path": str(historical_path.relative_to(settings.root)), "sha256": sha256_file(historical_path),
        "row_count": len(historical), "data_status": "READY", "freshness_status": "READY", "critical": True,
        "reliability": "PREVIOUS_SEASONS_ONLY", "quality_warnings": "Current and future seasons excluded"})

    # Ensure all immutable games can be assembled from their original pregame
    # bundles. No prior prediction is ever recomputed here.
    for extra in ("game_eligibility.csv", "game_refresh_provenance.csv", f"live_provider_manifest_{season}_w{week:02d}.csv"):
        if (refresh / extra).exists():
            if extra=="game_eligibility.csv" and elapsed_ids:
                eligibility.to_csv(inputs/extra,index=False,lineterminator="\n")
            else:
                shutil.copy2(refresh / extra, inputs / extra)
    lock_manifest = pd.DataFrame(rows)
    lock_manifest["selected_for_lock"] = True
    lock_manifest["locked_path"] = lock_manifest.snapshot_path
    lock_manifest["legacy_metadata_normalizations"] = int(missing_mask.sum())
    lock_manifest.to_csv(destination / "lock_manifest.csv", index=False, lineterminator="\n")
    blockers = lock_manifest[(lock_manifest.critical.astype(bool)) & lock_manifest.data_status.eq("BLOCKED")].source.tolist()
    lock_status = "BLOCKED" if blockers else ("READY_WITH_WARNINGS" if lock_manifest.data_status.ne("READY").any() else "READY")
    lock_meta = {"lock_id": lock_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cutoff_utc": cutoff_ts.isoformat(), "season": season, "week": week, "run_type": "UNPLAYED_ONLY",
        "snapshot_id": meta["refresh_id"], "lock_status": lock_status, "production_blockers": blockers,
        "fixture_cutoff_validation": "PASS", "fixture_cutoff_reasons": [], "feature_version": settings.phase2_feature_version,
        "model_version": settings.phase2_model_version, "manifest_sha256": sha256_file(destination / "lock_manifest.csv"),
        "architecture": "immutable game-keyed PRE_KICKOFF-only inputs", "prediction_scope": "UNPLAYED_ONLY",
        "eligible_game_ids": sorted(eligible), "game_refresh_manifest": str(manifest_path.relative_to(settings.root)),
        "game_refresh_manifest_sha256": sha256_file(manifest_path),
        "immutable_game_ids": sorted(immutable_ids),
        "game_eligibility_sha256": sha256_file(inputs / "game_eligibility.csv"),
        "pregame_player_features_sha256": sha256_file(candidate_path),
        "team_context_schedule_sha256": sha256_file(context_path),
        "historical_team_features_sha256": sha256_file(historical_path),
        "critical_source_states": [{"source": r.source, "source_state": r.source_state, "data_status": r.data_status,
            "retrieved_at_utc": r.retrieved_at_utc, "known_at_utc": r.known_at_utc, "sha256": r.sha256,
            "quality_warnings": r.quality_warnings} for r in lock_manifest[lock_manifest.source.isin(["injuries", "quarterbacks"])].itertuples(index=False)]}
    lock_meta.update({
        "legacy_metadata_normalizations": int(missing_mask.sum()),
        "partial_player_feature_compatibility": "PASS",
        "eligible_player_rows": int(len(candidates)),
        "native_configured_feature_version_rows": int(native_mask.sum()),
        "missing_version_rows_normalized": int(missing_mask.sum()),
        "wrong_non_null_feature_version_rows": int(wrong_mask.sum()),
        "player_model_input_features": model_input_audit["features"],
        "model_supported_missing_player_features": model_input_audit["missing_model_features"],
        "source_player_feature_artifact_sha256": player_source_sha256,
        "source_player_feature_schema_sha256": source_schema_sha256,
        "player_model_artifacts": ({key: value for key, value in model_contract.items() if key != "pipelines"}
                                    if model_contract else None),
    })
    lock_meta.update(inherited_team_meta)
    (destination / "data_lock.json").write_text(json.dumps(lock_meta, indent=2, sort_keys=True), encoding="utf-8")
    from nfl2026.production.core import verify_lock
    lock_errors = verify_lock(destination)
    if lock_errors:
        raise ValueError(f"New partial lock failed immediate verification: {lock_errors}")
    return destination, lock_manifest, lock_meta
    return dest, eligibility
