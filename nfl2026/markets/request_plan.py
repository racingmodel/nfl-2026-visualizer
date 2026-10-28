"""Read-only forecast lineage and fail-closed bookmaker request planning."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from nfl2026.utilities.io import sha256_file


def merged_forecast(settings, bundle: Path):
    # Import locally: the comparison engine also consumes this view.
    from nfl2026.markets.core import validate_prediction_reference
    anchor = validate_prediction_reference(bundle)
    partial = anchor.get("prediction_scope") == "UNPLAYED_ONLY"
    if partial:
        lineage = pd.read_csv(bundle / "game_prediction_manifest.csv")
    else:
        lineage = pd.read_csv(bundle / "games.csv", usecols=["game_id"])
        lineage["prediction_source_run_id"] = anchor["run_id"]
    if lineage.empty or lineage.game_id.duplicated().any():
        raise ValueError("Forecast lineage is empty or contains duplicate fixtures")
    games, players, sources, source_bundles = [], [], {}, {}
    for run_id in lineage.prediction_source_run_id.astype(str).unique():
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("Invalid forecast source identity")
        source = bundle.parent / run_id
        meta = validate_prediction_reference(source)
        if meta.get("run_id") != run_id or not {"games.csv", "players.csv"}.issubset(meta.get("file_hashes", {})):
            raise ValueError("Forecast source identity or immutable data hashes are missing")
        if (meta.get("season"), meta.get("week")) != (anchor.get("season"), anchor.get("week")):
            raise ValueError("Forecast source belongs to another week")
        source_games = pd.read_csv(source / "games.csv")
        source_players = pd.read_csv(source / "players.csv")
        ids = set(lineage.loc[lineage.prediction_source_run_id.eq(run_id), "game_id"].astype(str))
        selected = source_games[source_games.game_id.astype(str).isin(ids)].copy()
        if len(selected) != len(ids) or selected.game_id.duplicated().any():
            raise ValueError("Forecast source does not uniquely cover its lineage")
        for game in selected.itertuples(index=False):
            if pd.Timestamp(meta["created_at_utc"]) >= pd.Timestamp(game.kickoff_utc):
                raise ValueError("Forecast source was created after kickoff")
            sources[str(game.game_id)] = meta
        selected["prediction_source_run_id"] = run_id
        selected["prediction_reused_immutable"] = run_id != anchor["run_id"]
        games.append(selected)
        players.append(source_players[source_players.game_id.astype(str).isin(ids)])
        source_bundles[run_id] = (source_games, meta)
    games = pd.concat(games, ignore_index=True)
    players = pd.concat(players, ignore_index=True)
    if players.duplicated(["game_id", "player_id"]).any():
        raise ValueError("Merged forecast contains duplicate players")
    return anchor, games, players, sources, source_bundles


def local_fixture_states(settings, season, week, now, bundle=None):
    """A provisional offline plan; official requests always obtain fresh states."""
    from nfl2026.live.partial_week import classify_game_eligibility
    root = settings.root / "data/live_refresh" / str(season) / f"week_{week:02d}"
    choices = []
    for path in root.glob("*/game_refresh_manifest.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))
        observed = pd.Timestamp(meta["refresh_timestamp_utc"])
        if pd.isna(observed) or observed.tzinfo is None:
            raise ValueError("Local fixture snapshot requires an explicit observation timestamp")
        # Historical plans cannot consume a later refresh (including its final
        # scores). Live captures still recheck independent state before charging.
        if observed > pd.Timestamp(now):
            continue
        choices.append((observed, path, meta))
    if not choices:
        if bundle is None:
            raise ValueError("No certified local fixture-state snapshot; request scope cannot be planned")
        # Full-week forecasts have an immutable source lock rather than a
        # partial-refresh certificate. Use its independently captured schedule
        # only for a provisional offline plan; live capture still rechecks NFL
        # state before every chargeable request in scoped_fetch.
        from nfl2026.markets.core import validate_prediction_reference
        from nfl2026.production.core import verify_lock
        anchor=validate_prediction_reference(bundle)
        if anchor.get('prediction_scope')=='UNPLAYED_ONLY' or (anchor.get('season'),anchor.get('week'))!=(season,week):
            raise ValueError('Missing fixture-state certificate for this forecast scope')
        lock_id=str(anchor.get('lock_id',''))
        if not lock_id or Path(lock_id).name!=lock_id:
            raise ValueError('Invalid full-week lock identity')
        lock=settings.root/'locks'/str(season)/f'week_{week:02d}'/lock_id
        copied=bundle/'data_lock.json'
        if (not copied.is_file() or sha256_file(copied)!=anchor.get('file_hashes',{}).get('data_lock.json') or
            not (lock/'data_lock.json').is_file() or sha256_file(copied)!=sha256_file(lock/'data_lock.json') or verify_lock(lock)):
            raise ValueError('Full-week fixture lock is missing or hash-invalid')
        frozen=json.loads(copied.read_text())
        if (frozen.get('lock_status') not in {'READY','READY_WITH_WARNINGS'} or frozen.get('production_blockers')
            or (frozen.get('season'),frozen.get('week'))!=(season,week)
            or pd.Timestamp(frozen['cutoff_utc'])>pd.Timestamp(now)):
            raise ValueError('Full-week source lock is not certified READY')
        schedule=lock/'inputs/games.csv'
        provenance=pd.read_csv(lock/'lock_manifest.csv')
        schedule_source=provenance[provenance.source.eq('schedule')]
        if len(schedule_source)!=1:
            raise ValueError('Full-week schedule source provenance is ambiguous')
        states=classify_game_eligibility(pd.read_csv(schedule),season,week,now)
        return states,{'path':str(schedule),'sha256':sha256_file(schedule),'lock_id':lock_id,
            'lock_manifest_sha256':sha256_file(lock/'lock_manifest.csv'),
            'retrieved_at_utc':str(schedule_source.iloc[0].retrieved_at_utc),'provisional_offline':True,
            'evidence_basis':'hash-validated official full-week source lock; execution-time state not yet observed'}
    _, path, meta = max(choices, key=lambda item: item[0])
    schedule = path.parent / "games.csv"
    expected = meta.get("source_files", {}).get("games.csv")
    if not expected or sha256_file(schedule) != expected:
        raise ValueError("Local fixture schedule failed hash validation")
    states = classify_game_eligibility(pd.read_csv(schedule), season, week, now)
    # Partial refresh games.csv contains refreshable fixtures only. Preserve
    # certified LIVE/FINAL exclusions from its complete eligibility manifest.
    eligibility_path = path.parent / "game_eligibility.csv"
    eligibility_hash = meta.get("source_files", {}).get("game_eligibility.csv")
    if not eligibility_hash or sha256_file(eligibility_path) != eligibility_hash:
        raise ValueError("Local fixture eligibility failed hash validation")
    archived = pd.read_csv(eligibility_path)
    if archived.game_id.duplicated().any():
        raise ValueError("Duplicate archived fixture states")
    rows = states.set_index("game_id").to_dict("index")
    for old in archived.to_dict("records"):
        gid = str(old["game_id"])
        if old["eligibility_state"] in {"LIVE", "FINAL"}:
            rows[gid] = {key:value for key,value in old.items() if key != "game_id"}
        elif gid not in rows:
            rows[gid] = {**old, "eligibility_state":"AMBIGUOUS", "eligibility_reason":"No complete local schedule evidence"}
    states = pd.DataFrame([{"game_id":gid, **row} for gid,row in rows.items()])
    return states, {"path": str(schedule), "sha256": expected,
        "eligibility_path": str(eligibility_path), "eligibility_sha256": eligibility_hash,
        "retrieved_at_utc": meta["refresh_timestamp_utc"], "provisional_offline": True}


def eligible_fixtures(fixtures, states, now, *, margin_seconds=0):
    """Require matching teams, kickoff and an independently observed pregame state."""
    if states.game_id.astype(str).duplicated().any():
        raise ValueError("Duplicate fixture-state evidence")
    lookup = states.set_index(states.game_id.astype(str))
    rows = []
    for fixture in fixtures.to_dict("records"):
        gid = str(fixture["game_id"])
        reasons = []
        state = "AMBIGUOUS"
        if gid not in lookup.index:
            reasons.append("MISSING_INDEPENDENT_GAME_STATE")
        else:
            evidence = lookup.loc[gid]
            state = str(evidence.eligibility_state)
            if state != "PRE_KICKOFF":
                reasons.append(state + ": " + str(evidence.get("eligibility_reason", "")))
            for key in ("home_team", "away_team"):
                if str(evidence.get(key, "")) != str(fixture[key]):
                    reasons.append("CONFLICTING_" + key.upper())
            if pd.to_datetime(evidence.kickoff_utc, utc=True, errors="coerce") != pd.to_datetime(fixture["kickoff_utc"], utc=True, errors="coerce"):
                reasons.append("CONFLICTING_KICKOFF")
        kickoff = pd.to_datetime(fixture["kickoff_utc"], utc=True, errors="coerce")
        if pd.isna(kickoff) or kickoff <= pd.Timestamp(now) + pd.Timedelta(seconds=margin_seconds):
            reasons.append("STARTED_OR_WITHIN_REQUEST_SAFETY_WINDOW")
        if str(fixture.get("data_quality_status", "READY")).upper() == "BLOCKED":
            reasons.append("FORECAST_DATA_BLOCKED")
        fixture.update({"eligibility_state": state, "request_eligible": not reasons,
            "exclusion_reasons": reasons})
        rows.append(fixture)
    return pd.DataFrame(rows)


def request_plan(settings, bundle, now=None):
    from nfl2026.markets.providers.the_odds_api import TheOddsApiProvider
    now = now or datetime.now(timezone.utc)
    meta, games, _, _, _ = merged_forecast(settings, bundle)
    states, evidence = local_fixture_states(settings, int(meta["season"]), int(meta["week"]), now, bundle=bundle)
    planned = eligible_fixtures(games, states, now, margin_seconds=25)
    eligible = planned[planned.request_eligible]
    prop_ids = eligible.loc[(pd.to_datetime(eligible.kickoff_utc, utc=True) <= pd.Timestamp(now) + pd.Timedelta(hours=settings.market_prop_window_hours)) | (not settings.market_low_credit_mode), "game_id"].tolist()
    game_cost = TheOddsApiProvider.estimate_cost(TheOddsApiProvider.GAME_MARKETS, len(settings.market_bookmakers), len(settings.market_regions.split(","))) if len(eligible) else 0
    prop_cost = TheOddsApiProvider.estimate_cost(TheOddsApiProvider.PROP_MARKETS, len(settings.market_bookmakers), len(settings.market_regions.split(",")))
    columns = ["game_id", "kickoff_utc", "away_team", "home_team", "eligibility_state", "prediction_source_run_id", "prediction_reused_immutable", "exclusion_reasons"]
    return {"status": "DRY_RUN_REQUEST_PLAN", "network_requests": 0, "external_requests": 0,
        "prediction_run_id": meta["run_id"], "prediction_scope": meta.get("prediction_scope", "FULL_WEEK"),
        "prediction_metadata_sha256":sha256_file(bundle / "metadata.json"),
        "prediction_lineage_sha256":meta.get("file_hashes", {}).get("game_prediction_manifest.csv"),
        "planned_at_utc": now.isoformat(), "fixture_state_evidence": evidence,
        "eligible_fixtures": eligible[columns].to_dict("records"),
        "excluded_fixtures": planned.loc[~planned.request_eligible, columns].to_dict("records"),
        "merged_forecast_games": len(games), "bookmakers": list(settings.market_bookmakers),
        "eligibility_counts": planned.eligibility_state.value_counts().to_dict(),
        "game_markets": TheOddsApiProvider.GAME_MARKETS.split(","),
        "prop_markets": TheOddsApiProvider.PROP_MARKETS.split(","), "prop_fixture_ids": prop_ids,
        "game_markets_credits": game_cost, "player_props_max_credits": len(prop_ids) * prop_cost,
        "expected_credits": game_cost + len(prop_ids) * prop_cost,
        "quota_reserve": settings.market_quota_reserve, "event_discovery_credits": 0,
        "maximum_chargeable_requests": (1 if len(eligible) else 0) + len(prop_ids),
        "note": "Provisional local state only. Official capture revalidates independent states, exact provider event mappings and quota before every chargeable request; preserved forecasts are not refreshed predictions."}
