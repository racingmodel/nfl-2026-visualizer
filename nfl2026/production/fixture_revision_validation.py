"""Strict fixture-revision source and prediction contracts.

These checks deliberately do not certify an entire week, infer health from a
missing name, or waive the historical QB certificate introduced in Phase 1.
"""
from pathlib import Path
import json

import joblib
import numpy as np
import pandas as pd

from nfl2026.features.qb_identity_archive import require_identity_archive
from nfl2026.features.qb_archive_certification import validate_identity_certificate
from nfl2026.production.core import verify_lock, SOURCE_SPECS
from nfl2026.registry.core import load_registered_model
from nfl2026.utilities.io import sha256_file


def utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Explicit UTC-aware evidence time required")
    return stamp.tz_convert("UTC")


def true(value):
    return value is True or str(value).lower() in {"true", "1"}


def read(path):
    return pd.read_csv(path, low_memory=False)


def eligible_states(states, ids, now, expected=None):
    required = {"game_id", "eligibility_state", "kickoff_utc", "away_team", "home_team"}
    if not required.issubset(states) or states.game_id.duplicated().any():
        raise ValueError("Independent fixture states missing/duplicated")
    selected = states[states.game_id.astype(str).isin(ids)]
    if set(selected.game_id.astype(str)) != set(ids):
        raise ValueError("Selected fixture absent from independent schedule")
    for row in selected.itertuples():
        if row.eligibility_state != "PRE_KICKOFF" or utc(now) >= utc(row.kickoff_utc)-pd.Timedelta(minutes=1):
            raise ValueError(f"Fixture {row.game_id} is CLOSED to revision: {row.eligibility_state}/cutoff")
        if expected:
            old = expected[str(row.game_id)]
            if any(str(getattr(row, c)) != str(old[c]) for c in ["away_team", "home_team"]) or utc(row.kickoff_utc) != utc(old["kickoff_utc"]):
                raise ValueError("Fixture kickoff/team identity changed; a new request is required")
    return selected


def registered_contracts(settings, model_ids, feature_version):
    registry = settings.artifact_root/"registry/model_registry.csv"
    contracts = {}
    if set(model_ids) != {"team", "player", "td"}:
        raise ValueError("Approved model identities incomplete")
    for key, name in [("team", "team_score_model"), ("player", "player_opportunity_model"), ("td", "player_td_model")]:
        identity = model_ids[key]
        if identity["name"] != name:
            raise ValueError("Approved model name mismatch")
        path, row = load_registered_model(registry, name, identity["version"], feature_version)
        if row["artifact_sha256"] != identity["hash"]:
            raise ValueError("Approved model artifact changed; no silent promotion")
        payload = joblib.load(path)
        features = payload["features"]
        if not features or len(features) != len(set(features)):
            raise ValueError("Registered model feature schema invalid")
        contracts[key] = {"path": str(path), "hash": identity["hash"], "features": list(features)}
    return contracts


def validate_sources(settings, lock, ids, now, fixtures, model_ids, feature_version):
    """Assess only requested fixtures; retain strict source and model parity."""
    lock = Path(lock)
    errors = verify_lock(lock)
    meta = json.loads((lock/"data_lock.json").read_text(encoding="utf-8"))
    if errors or sha256_file(lock/"lock_manifest.csv") != meta.get("manifest_sha256"):
        raise ValueError("Source lock integrity failed: "+str(errors))
    aggregate_blockers = set(meta.get("production_blockers", []))
    scoped_providers = {"roster", "injuries", "depth", "quarterbacks"}
    if meta.get("lock_status") not in {"READY", "BLOCKED"} or aggregate_blockers-scoped_providers:
        raise ValueError("Source lock has unresolved non-fixture production blockers")
    if meta.get("lock_status") == "BLOCKED" and not aggregate_blockers:
        raise ValueError("Unexplained blocked source lock")
    cutoff = utc(meta["cutoff_utc"])
    if cutoff > utc(now) or feature_version != settings.phase2_feature_version:
        raise ValueError("Source cutoff/model feature contract incompatible")
    inputs = lock/"inputs"
    contracts = registered_contracts(settings, model_ids, feature_version)
    games = read(inputs/"games.csv")
    games = games[games.game_id.astype(str).isin(ids)].copy()
    if set(games.game_id.astype(str)) != set(ids) or games.game_id.duplicated().any():
        raise ValueError("Source lock fixture population differs")
    season, week = int(meta["season"]), int(meta["week"])
    teams = set(games.home_team)|set(games.away_team)
    if len(teams) != 2*len(games):raise ValueError("Requested fixture/team joins are ambiguous")
    if games.season.ne(season).any() or games.week.ne(week).any():
        raise ValueError("Source season/week mismatch")
    from nfl2026.live.partial_week import classify_game_eligibility
    locked_states = classify_game_eligibility(games,season,week,utc(now))
    if not locked_states.eligibility_state.eq("PRE_KICKOFF").all():
        raise ValueError("Locked source already marks a requested fixture LIVE/FINAL/ambiguous")
    for game in games.itertuples():
        original = fixtures[str(game.game_id)]
        if str(game.home_team) != original["home_team"] or str(game.away_team) != original["away_team"] or utc(game.kickoff_utc) != utc(original["kickoff_utc"]):
            raise ValueError("Source fixture identity differs from original")
        if cutoff >= utc(game.kickoff_utc)-pd.Timedelta(minutes=1):
            raise ValueError("Source lock cutoff expired")
    if any(games[c].notna().any() for c in ["home_score", "away_score", "result", "total", "overtime"] if c in games):
        raise ValueError("Requested fixture contains outcome leakage")
    roster = read(inputs/f"weekly_roster_{season}.csv")
    roster = roster[roster.team.isin(teams)&roster.week.eq(week)&roster.season.eq(season)]
    if roster.empty or roster.gsis_id.isna().any() or roster.gsis_id.duplicated().any():
        raise ValueError("Roster identity/eligibility missing or ambiguous")
    if "transaction_reconciliation_error" in roster and roster.transaction_reconciliation_error.fillna("").ne("").any():
        raise ValueError("Current roster/transaction contradiction")
    injuries = read(inputs/f"injuries_{season}.csv")
    injuries = injuries[injuries.team.isin(teams)]
    if (not {"season","week"}.issubset(injuries)
            or pd.to_numeric(injuries.season,errors="coerce").ne(season).any()
            or pd.to_numeric(injuries.week,errors="coerce").ne(week).any()):
        raise ValueError("Injury source exact season/week identity missing or conflicting")
    if "game_id" in injuries:
        fixture_by_team = {team:str(game.game_id) for game in games.itertuples() for team in [game.home_team,game.away_team]}
        named = injuries[injuries.game_id.notna()&injuries.game_id.astype(str).ne("")]
        if any(str(r.game_id) != fixture_by_team[r.team] for r in named.itertuples()):
            raise ValueError("Injury source exact fixture join conflicts")
    reports = injuries[injuries.record_type.eq("PLAYER_REPORT")]
    for report in reports.itertuples():
        match = roster[roster.team.eq(report.team)&roster.gsis_id.astype(str).eq(str(report.player_id))]
        if len(match) != 1 or (hasattr(report,"player_name") and report.player_name != match.iloc[0].full_name):
            raise ValueError("Injury player/team/roster identity unresolved")
    qbs = read(inputs/f"qb_status_{season}_w{week:02d}.csv")
    qbs = qbs[qbs.game_id.astype(str).isin(ids)]
    depth = read(inputs/f"depth_charts_{season}.csv")
    depth = depth[depth.team.isin(teams)]
    provider_manifest = read(lock/"lock_manifest.csv")
    provenance_path = inputs/"game_refresh_provenance.csv"
    provenance = read(provenance_path) if provenance_path.is_file() else pd.DataFrame()
    clocks = []
    for provider in ["roster", "injuries", "depth", "quarterbacks", "transactions"]:
        rows = provider_manifest[provider_manifest.source.eq(provider)]
        if len(rows) != 1:
            raise ValueError(provider+" source validation incomplete")
        row = rows.iloc[0]
        if not provenance.empty and provider in scoped_providers:
            proof = provenance[provenance.provider.eq(provider)&provenance.game_id.astype(str).isin(ids)]
            if len(proof) != len(ids) or proof.game_id.duplicated().any() or not proof.source_state.eq("VALID").all():
                raise ValueError(provider+" requested-fixture source proof incomplete")
            for p in proof.itertuples():
                artifact = inputs/Path(str(p.artifact)).name
                known = utc(p.source_known_at_utc); observed = utc(p.refresh_timestamp_utc)
                if (not artifact.is_file() or sha256_file(artifact) != p.artifact_sha256 or known > observed or observed > cutoff
                        or utc(now)-known > pd.Timedelta(hours=min(SOURCE_SPECS[provider]["max_age_hours"],36))):
                    raise ValueError(provider+" requested-fixture proof hash/clock invalid")
                clocks.append({"provider":provider,"game_id":p.game_id,"known_at_utc":known.isoformat(),"observed_at_utc":observed.isoformat(),"sha256":p.artifact_sha256})
            continue
        if provider in aggregate_blockers or str(row.source_state) != "VALID" or str(row.data_status) not in {"READY", "READY_WITH_WARNINGS"}:
            raise ValueError(provider+" source validation incomplete; independent scoped proof required")
        known = utc(row.get("known_at_utc", row.get("source_known_at_utc")))
        observed = utc(row.retrieved_at_utc)
        if known > observed or observed > cutoff or utc(now)-known > pd.Timedelta(hours=min(SOURCE_SPECS[provider]["max_age_hours"], 36)):
            raise ValueError(provider+" source clock stale, missing or contradictory")
        clocks.append({"provider": provider, "known_at_utc": known.isoformat(), "observed_at_utc": observed.isoformat(), "sha256": str(row.sha256)})
    for game in games.itertuples():
        for team in [game.away_team, game.home_team]:
            coverage = injuries[injuries.team.eq(team)&injuries.record_type.eq("TEAM_COVERAGE")]
            if len(coverage) != 1 or coverage.iloc[0].source_state != "VALID" or not true(coverage.iloc[0].get("complete_final_club_report", False)):
                raise ValueError(team+" complete final injury source required; practice/absence never clears")
            if utc(now) >= utc(game.kickoff_utc)-pd.Timedelta(minutes=90) and not true(coverage.iloc[0].get("complete_inactive_club_report", False)):
                raise ValueError(team+" own inactive list not certified inside the inactive window")
            qb = qbs[qbs.team.eq(team)&qbs.game_id.eq(game.game_id)]
            if len(qb) != 1:
                raise ValueError(team+" QB identity/source missing")
            qb = qb.iloc[0]
            expected_role_proof = true(qb.get("schedule_depth_agreement",False)) or (
                qb.status == "CONFIRMED" and true(qb.get("starter_confirmed",False)) and true(qb.get("official_starter_supersedes_depth",False)))
            if qb.source_state != "VALID" or qb.status not in {"EXPECTED", "VALIDATED", "CONFIRMED"} or qb.confidence != "HIGH" or not true(qb.active_roster_match) or not expected_role_proof:
                raise ValueError(team+" QB source/role unresolved")
            if str(qb.injury_status).upper() in {"QUESTIONABLE", "DOUBTFUL", "OUT", "IR", "PUP", "INACTIVE", "SUSPENDED"}:
                raise ValueError(team+" QB adverse/uncertain availability")
            match = roster[roster.team.eq(team)&roster.gsis_id.astype(str).eq(str(qb.qb_player_id))&roster.position.eq("QB")]
            if len(match) != 1 or match.iloc[0].status != "ACT" or str(match.iloc[0].get("current_transaction_state", "")).upper() in {"IR", "WAIVED", "RELEASED", "PRACTICE_SQUAD", "SUSPENDED"}:
                raise ValueError(team+" QB roster eligibility unresolved")
            if not (depth.team.eq(team)&depth.position.eq("QB")&depth.player_id.astype(str).eq(str(qb.qb_player_id))).any():
                raise ValueError(team+" QB current depth proof absent")
            for field in ["known_at_utc", "retrieved_at_utc"]:
                if utc(qb[field]) > cutoff:
                    raise ValueError("QB evidence after source cutoff")
    candidates = read(inputs/"player_pregame_universe_features.csv")
    candidates = candidates[candidates.game_id.astype(str).isin(ids)]
    if candidates.empty or candidates.duplicated(["game_id", "player_id"]).any() or set(candidates.team)-teams:
        raise ValueError("Player-feature identity population incomplete")
    if candidates.feature_version.astype(str).ne(feature_version).any():
        raise ValueError("Player-feature version incompatible")
    if any(str(c).startswith(("target_", "actual_", "pbp_")) for c in candidates):
        raise ValueError("Target player matrix contains outcome columns")
    for column in ["source_latest_week", "source_actuals_latest_week"]:
        if column in candidates and pd.to_numeric(candidates[column], errors="coerce").ge(week).any():
            raise ValueError("Target-week outcome history in player features")
    for game in games.itertuples():
        block = candidates[candidates.game_id.eq(game.game_id)]
        if block.empty or not {game.home_team, game.away_team}.issubset(set(block.team)):
            raise ValueError("Player features do not cover both exact fixture teams")
        for player in block.itertuples():
            expected_opponent = game.home_team if player.team == game.away_team else game.away_team
            if player.team not in {game.home_team, game.away_team} or player.opponent != expected_opponent:
                raise ValueError("Player feature game/team/opponent join conflicts")
    for frame in [candidates, qbs, injuries, depth, roster]:
        for column in ["known_at_utc", "retrieved_at_utc", "prediction_cutoff_utc", "effective_utc", "roster_source_known_at_utc"]:
            if column in frame:
                for value in frame[column].dropna():
                    if str(value).strip() and utc(value) > cutoff:
                        raise ValueError("Row-level source/feature clock after cutoff")
    for column in ["prediction_cutoff_utc"]:
        if column not in candidates or candidates[column].isna().any():
            raise ValueError("Player pregame provenance missing")
    for key in ["player", "td"]:
        if set(contracts[key]["features"])-set(candidates):
            raise ValueError("Player model input schema incompatible")
    historical_path = inputs/"historical_team_features.csv"
    historical = read(historical_path)
    require_identity_archive(historical)
    validate_identity_certificate(historical, historical_path, model_ids["team"]["hash"], settings.root)
    if set(contracts["team"]["features"])-set(historical):
        raise ValueError("Team model input schema incompatible")
    if historical.season.ge(season).any() or pd.to_datetime(historical.kickoff_utc, utc=True).ge(cutoff).any():
        raise ValueError("Historical feature archive leaks current/future outcomes")
    team_state = None
    if week >= 4:
        state_path = inputs/f"team_feature_state_{season}_w{week:02d}.csv"
        state_meta = json.loads((inputs/f"team_feature_state_{season}_w{week:02d}.json").read_text(encoding="utf-8"))
        if (state_meta.get("state_file_sha256") != sha256_file(state_path)
                or state_meta.get("feature_version") != feature_version
                or int(state_meta.get("season", -1)) != season or int(state_meta.get("target_week", -1)) != week
                or int(state_meta.get("latest_completed_source_week", week)) >= week
                or utc(state_meta["generated_at_utc"]) > cutoff):
            raise ValueError("PIT team feature state manifest incompatible or target-week leakage")
        team_state = read(state_path)
        team_state = team_state[team_state.game_id.astype(str).isin(ids)]
        for column in ["source_latest_week", "source_actuals_latest_week"]:
            if column in team_state and pd.to_numeric(team_state[column],errors="coerce").ge(week).any():
                raise ValueError("Target-week outcome history in team state")
    # Run pure feature construction and the fitted missing-value transformers
    # during preflight; no estimator prediction or zero imputation occurs.
    from scripts.build_phase2 import _current_team_matrix
    from nfl2026.live.partial_week import _validate_partial_player_input_contract
    matrix = _current_team_matrix(games,historical,contracts["team"]["features"],team_state=team_state,target_week=week)
    tmodel = joblib.load(contracts["team"]["path"])
    _validate_partial_player_input_contract(matrix,{"features":contracts["team"]["features"],"pipelines":[("home",tmodel["home"]),("away",tmodel["away"])]})
    pmodel = joblib.load(contracts["player"]["path"]); tdmodel = joblib.load(contracts["td"]["path"])
    _validate_partial_player_input_contract(candidates,{"features":contracts["player"]["features"],"pipelines":[(name,model) for name,model in pmodel["models"].items() if not isinstance(model,dict)]})
    _validate_partial_player_input_contract(candidates,{"features":contracts["td"]["features"],"pipelines":[("td",tdmodel["model"])]})
    return {"status": "PASS", "fixture_ids": sorted(ids), "source_cutoff_utc": cutoff.isoformat(), "source_timestamps": clocks,
            "aggregate_blockers_retained": sorted(aggregate_blockers),
            "feature_hashes": {name: sha256_file(inputs/name) for name in ["historical_team_features.csv", "player_pregame_universe_features.csv"]},
            "model_contracts": contracts, "current_matrix_schema_and_trained_missing_value_contract": "PASS",
            "health_inferred_from_absence": False, "historical_QB_certificate_required": True}


def validate_coherence(games, players, expected_fixtures):
    """Independent checks on joint team/player outputs, never accuracy-selected."""
    ids = set(expected_fixtures)
    if games.game_id.duplicated().any() or set(games.game_id.astype(str)) != ids or set(players.game_id.astype(str)) != ids:
        raise ValueError("Prediction fixture population differs")
    if players.duplicated(["game_id", "player_id"]).any() or players.player_id.isna().any():
        raise ValueError("Prediction player identities duplicated/missing")
    for game in games.itertuples():
        old = expected_fixtures[str(game.game_id)]
        if (game.home_team, game.away_team) != (old["home_team"], old["away_team"]) or utc(game.kickoff_utc) != utc(old["kickoff_utc"]):
            raise ValueError("Prediction team/kickoff identity mismatch")
        if not np.isfinite([game.home_expected_points, game.away_expected_points, game.home_win_probability, game.away_win_probability]).all():
            raise ValueError("Non-finite team prediction")
        if min(game.home_expected_points, game.away_expected_points) < 0 or max(game.home_expected_points, game.away_expected_points) > 60:
            raise ValueError("Team score bounds invalid")
        if not (0 <= game.home_win_probability <= 1 and 0 <= game.away_win_probability <= 1) or abs(game.home_win_probability+game.away_win_probability-1) > 1e-10:
            raise ValueError("Team probability coherence failed")
        for team, opponent, points in [(game.home_team, game.away_team, game.home_expected_points), (game.away_team, game.home_team, game.away_expected_points)]:
            group = players[players.game_id.eq(game.game_id)&players.team.eq(team)]
            if group.empty or group.opponent.ne(opponent).any():
                raise ValueError("Player/team/opponent population incoherent")
            if not group.position.isin(["QB", "RB", "FB", "WR", "TE"]).all():
                raise ValueError("Unsupported prediction player position")
            if not group.roster_status.eq("ACT").all() or group.injury_status.isin(["OUT", "IR", "PUP", "INACTIVE", "SUSPENDED"]).any():
                raise ValueError("Ineligible player in revision")
            for column in ["expected_carries", "expected_targets", "expected_rush_yards", "expected_receiving_yards", "raw_td_probability", "reconciled_td_probability"]:
                values = pd.to_numeric(group[column], errors="coerce")
                if not np.isfinite(values).all() or values.lt(0).any() or ("probability" in column and values.gt(1).any()):
                    raise ValueError("Player numeric/probability bounds invalid")
            target = min(len(group), float(np.clip(points/7*.72, .35, 4.25)))
            if abs(group.reconciled_td_probability.sum()-target) > 1e-6:
                raise ValueError("Player TD reconciliation incompatible with team scores")
            if 0 < target < len(group):
                raw = np.clip(group.raw_td_probability.to_numpy(dtype=float),1e-6,1-1e-6)
                adjusted = group.reconciled_td_probability.to_numpy(dtype=float)
                if ((adjusted <= 0)|(adjusted >= 1)).any():raise ValueError("TD reconciliation endpoint inconsistent")
                shifts = np.log(adjusted/(1-adjusted))-np.log(raw/(1-raw))
                if np.ptp(shifts) > 1e-6:raise ValueError("Player TD odds shifts inconsistent with joint reconciliation")
        if set(players[players.game_id.eq(game.game_id)].team) != {game.home_team, game.away_team}:
            raise ValueError("Extra player team in fixture")
    return {"status": "PASS", "policy": "JOINT_TEAM_AND_PLAYERS", "fixture_count": len(games), "player_count": len(players), "td_score_tolerance": 1e-6}
