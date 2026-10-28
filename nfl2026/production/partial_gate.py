"""Fail-closed fixture certification for immutable partial-week runs only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil

import pandas as pd

from nfl2026.live.partial_week import classify_game_eligibility
from nfl2026.utilities.io import sha256_file

GATE_VERSION = "partial-fixture-gate-v1"
CRITICAL_PROVIDERS = ("roster", "injuries", "depth", "quarterbacks")
# Existing official partial runs retain their original validation contract.
# This is a fixed policy introduction date, never a moving "today" exception.
LEGACY_PARTIAL_BEFORE_UTC = pd.Timestamp("2026-10-18T00:00:00Z")


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def fetch_fixture_states(season, week):
    """Read only the independent schedule. Scores classify state, never enter features."""
    from nfl2026.live.pipeline import ASSETS
    from nfl2026.live.providers.base import fetch_bytes
    payload = fetch_bytes(ASSETS["games.csv"], attempts=1, timeout_seconds=15)
    now = datetime.now(timezone.utc)
    states = classify_game_eligibility(pd.read_csv(BytesIO(payload), low_memory=False), season, week, now)
    return states, {"source_url": ASSETS["games.csv"], "retrieved_at_utc": now.isoformat(),
                    "sha256": hashlib.sha256(payload).hexdigest()}


def assess_partial_lock(lock: Path, now: datetime, states: pd.DataFrame | None = None) -> dict:
    """Read-only fixture assessment; a saved refresh is not a live-state certification."""
    from nfl2026.production.core import SOURCE_SPECS, filter_current_candidates, verify_lock
    meta = json.loads((lock / "data_lock.json").read_text())
    if meta.get("prediction_scope") != "UNPLAYED_ONLY":
        raise ValueError("Fixture isolation is restricted to partial-week locks")
    errors = verify_lock(lock)
    if sha256_file(lock/"lock_manifest.csv")!=meta.get("manifest_sha256"):
        errors.append("lock manifest hash mismatch")
    for key,name in [("game_eligibility_sha256","game_eligibility.csv"),
                     ("pregame_player_features_sha256","player_pregame_universe_features.csv"),
                     ("team_context_schedule_sha256","team_context_schedule.csv"),
                     ("historical_team_features_sha256","historical_team_features.csv")]:
        if sha256_file(lock/"inputs"/name)!=meta.get(key):
            errors.append(f"{name} metadata hash mismatch")
    if errors:
        raise ValueError(f"Partial lock integrity failed: {errors}")
    inputs = lock / "inputs"
    eligibility = pd.read_csv(inputs / "game_eligibility.csv")
    if eligibility.game_id.duplicated().any():
        raise ValueError("Duplicate eligibility IDs")
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        raise ValueError("Fixture assessment requires a UTC-aware time")
    cutoff = pd.Timestamp(meta["cutoff_utc"])
    season, week = int(meta["season"]), int(meta["week"])
    read = lambda name: pd.read_csv(inputs / name, low_memory=False)
    games = read("games.csv")
    injuries = read(f"injuries_{season}.csv")
    qbs = read(f"qb_status_{season}_w{week:02d}.csv")
    roster = read(f"weekly_roster_{season}.csv")
    candidates = read("player_pregame_universe_features.csv")
    depth = read(f"depth_charts_{season}.csv")
    transactions = read(f"transactions_{season}.csv")
    provenance = read("game_refresh_provenance.csv")
    context=read("team_context_schedule.csv")
    state_path=inputs/f"team_feature_state_{season}_w{week:02d}.csv"
    team_state=read(state_path.name) if state_path.exists() else None
    allowed = set(map(str, meta["eligible_game_ids"]))
    state_lookup = {} if states is None else states.set_index("game_id").to_dict("index")
    if states is not None and states.game_id.duplicated().any():
        raise ValueError("Current schedule has duplicate fixture IDs")
    results = []
    for entry in eligibility.to_dict("records"):
        gid = str(entry["game_id"])
        reasons = []
        state = str(entry["eligibility_state"])
        kickoff = pd.to_datetime(entry["kickoff_utc"], utc=True, errors="coerce")
        current = state_lookup.get(gid)
        if states is not None:
            if current is None:
                reasons.append("fixture absent from current independent schedule")
                state = "AMBIGUOUS"
            else:
                state = str(current["eligibility_state"])
                if pd.Timestamp(current["kickoff_utc"]) != kickoff:
                    reasons.append("kickoff changed since immutable source lock; refresh required")
        if pd.isna(kickoff):
            reasons.append("kickoff unavailable")
            state = "AMBIGUOUS"
        elif kickoff <= now and state == "PRE_KICKOFF":
            state = "LIVE"
        if str(entry["eligibility_state"]) in {"LIVE", "FINAL"}:
            # Never make a previously played fixture eligible again.
            state = str(entry["eligibility_state"])
        if state != "PRE_KICKOFF":
            reasons.append(f"fixture state {state}; original forecast retained")
        if gid in allowed and state == "PRE_KICKOFF":
            fixture = games[games.game_id.astype(str).eq(gid)]
            teams = {str(entry["away_team"]), str(entry["home_team"])}
            if len(fixture) != 1 or fixture[["away_score", "home_score"]].notna().any().any():
                reasons.append("locked fixture missing, duplicated or contains outcome data")
            fixture_context=context[context.game_id.astype(str).eq(gid)]
            if len(fixture_context)!=1:
                reasons.append("PIT team context missing or duplicated")
            elif any(fixture_context[c].notna().any() for c in ["away_score","home_score","result","total","overtime"] if c in fixture_context):
                reasons.append("PIT team context contains target-week outcome data")
            if week>=4:
                state_rows=(team_state[team_state.game_id.astype(str).eq(gid)]
                            if team_state is not None and "game_id" in team_state else pd.DataFrame())
                if (len(state_rows)!=1 or not {"home_team","away_team"}.issubset(state_rows)
                    or {str(state_rows.iloc[0].home_team),str(state_rows.iloc[0].away_team)}!=teams):
                    reasons.append("locked team feature coverage missing or conflicting")
            for provider in CRITICAL_PROVIDERS:
                records = provenance[provenance.game_id.astype(str).eq(gid) & provenance.provider.eq(provider)]
                if len(records) != 1:
                    reasons.append(f"{provider} provenance missing or duplicated")
                    continue
                record = records.iloc[0]
                if record.source_state != "VALID":
                    reasons.append(f"{provider} source state {record.source_state}")
                source = inputs / str(record.artifact)
                if not source.is_file() or sha256_file(source) != record.artifact_sha256:
                    reasons.append(f"{provider} provenance hash mismatch")
                known = pd.to_datetime(record.source_known_at_utc, utc=True, errors="coerce")
                retrieved = pd.to_datetime(record.refresh_timestamp_utc, utc=True, errors="coerce")
                if pd.isna(known) or pd.isna(retrieved) or known > cutoff or retrieved > cutoff:
                    reasons.append(f"{provider} evidence missing timestamp or post-cutoff")
                elif (now - known).total_seconds() > SOURCE_SPECS[provider]["max_age_hours"] * 3600:
                    reasons.append(f"{provider} evidence stale (over {SOURCE_SPECS[provider]['max_age_hours']} hours)")
            for team in sorted(teams):
                coverage = injuries[injuries.team.eq(team) & injuries.record_type.eq("TEAM_COVERAGE")]
                if len(coverage) != 1 or str(coverage.iloc[0].source_state) != "VALID":
                    reasons.append(f"{team} injury source missing, conflicting or invalid")
                qb = qbs[qbs.game_id.astype(str).eq(gid) & qbs.team.eq(team)]
                if len(qb) != 1:
                    reasons.append(f"{team} QB evidence missing or conflicting ({len(qb)} rows)")
                    continue
                qb = qb.iloc[0]
                if str(qb.source_state) != "VALID" or str(qb.status) not in {"EXPECTED", "VALIDATED", "CONFIRMED"}:
                    reasons.append(f"{team} QB source/state {qb.source_state}/{qb.status}")
                if str(qb.injury_status).upper() in {"QUESTIONABLE", "DOUBTFUL", "OUT", "IR", "PUP", "INACTIVE"}:
                    reasons.append(f"{team} expected QB availability {str(qb.injury_status).upper()}")
                # UNKNOWN individual health stays UNKNOWN under the existing provider contract.
                if str(qb.get("active_roster_match", "")).lower() not in {"true", "1"}:
                    reasons.append(f"{team} QB active roster match unresolved")
                active = roster[roster.team.eq(team) & roster.week.eq(week) & roster.status.eq("ACT")]
                if not active.gsis_id.astype(str).eq(str(qb.qb_player_id)).any():
                    reasons.append(f"{team} expected QB absent from active locked roster")
                team_depth = depth[depth.team.eq(team)]
                if team_depth.empty or not team_depth.player_id.astype(str).eq(str(qb.qb_player_id)).any():
                    reasons.append(f"{team} current depth evidence missing")
            # A correct provider summary cannot excuse contradictory row timestamps.
            for label, frame in [("injuries", injuries[injuries.team.isin(teams)]),
                                 ("roster", roster[roster.team.isin(teams)]),
                                 ("quarterbacks", qbs[qbs.game_id.astype(str).eq(gid)]),
                                 ("depth", depth[depth.team.isin(teams)]),
                                 ("player features", candidates[candidates.game_id.astype(str).eq(gid)])]:
                for column in ["known_at_utc", "source_known_at_utc", "prediction_cutoff_utc", "effective_utc"]:
                    if column not in frame:
                        continue
                    values=frame[column].dropna()
                    times=pd.to_datetime(values,utc=True,errors="coerce",format="mixed")
                    if times.isna().any() or times.gt(cutoff).any():
                        reasons.append(f"{label} {column} invalid or post-cutoff")
            try:
                scoped = candidates[candidates.game_id.astype(str).eq(gid)]
                if scoped.empty or scoped.duplicated(["game_id", "player_id"]).any():
                    raise ValueError("player feature coverage empty or duplicated")
                scoped = filter_current_candidates(scoped, roster[roster.team.isin(teams)],
                                                   injuries[injuries.team.isin(teams)], week, transactions)
                if not teams.issubset(set(scoped.team.astype(str))):
                    raise ValueError("eligible player coverage missing for one or both teams")
            except (ValueError, KeyError) as exc:
                reasons.append(f"roster/player eligibility: {exc}")
        elif gid not in allowed and state == "PRE_KICKOFF":
            reasons.append("fixture outside immutable lock refresh scope")
        results.append({"game_id": gid, "state": state, "kickoff_utc": str(kickoff),
                        "status": "ELIGIBLE" if not reasons else "BLOCKED",
                        "reasons": list(dict.fromkeys(reasons))})
    return {"version": GATE_VERSION, "assessed_at_utc": now.isoformat(),
            "base_lock_id": meta["lock_id"], "base_lock_sha256": sha256_file(lock / "data_lock.json"),
            "source_cutoff_utc": meta["cutoff_utc"], "fixtures": results,
            "eligible_game_ids": sorted(r["game_id"] for r in results if r["status"] == "ELIGIBLE")}


def prepare_partial_inference_lock(settings, base: Path) -> Path:
    """Certify a subset in a NEW lock; even an all-blocked attempt retains its audit."""
    meta = json.loads((base / "data_lock.json").read_text())
    try:
        states, evidence = fetch_fixture_states(int(meta["season"]), int(meta["week"]))
    except Exception as exc:
        states=pd.read_csv(base/"inputs/game_eligibility.csv")
        states["eligibility_state"]="AMBIGUOUS"
        evidence={"error":str(exc),"source_state":"ERROR"}
    now = datetime.now(timezone.utc)
    audit = assess_partial_lock(base, now, states)
    audit["state_evidence"] = evidence
    if evidence.get("error"):
        for fixture in audit["fixtures"]:
            fixture["reasons"].append("current fixture-state retrieval failed: "+evidence["error"])
    identity = hashlib.sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest()[:12]
    lock_id = f"{meta['lock_id']}-fixtures-{identity}"
    destination = base.parent / lock_id
    audit_root=settings.report_root/"partial_fixture_gates"/str(meta["season"])/f"week_{int(meta['week']):02d}"
    audit_root.mkdir(parents=True,exist_ok=True)
    audit_path=audit_root/(lock_id+".json")
    with audit_path.open("x",encoding="utf-8") as stream:
        json.dump(audit,stream,indent=2,sort_keys=True)
    eligible = set(audit["eligible_game_ids"])
    if not eligible:
        raise ValueError(f"All partial fixtures blocked; originals retained. Audit: {audit_path}")
    # Every omitted fixture must have a real, valid original pregame forecast.
    from nfl2026.production.evaluation import select_evaluation_runs
    originals = select_evaluation_runs(settings, int(meta["season"]), int(meta["week"]), now)
    omitted = {f["game_id"] for f in audit["fixtures"]} - eligible
    if omitted - set(originals.game_id.astype(str)):
        raise ValueError(f"Missing original forecasts for omitted fixtures: {sorted(omitted-set(originals.game_id.astype(str)))}")
    from nfl2026.production.core import validate_bundle
    for run_id in originals.loc[originals.game_id.astype(str).isin(omitted),"selected_run_id"].unique():
        original=settings.prediction_root/str(meta["season"])/f"week_{int(meta['week']):02d}"/str(run_id)
        checks=validate_bundle(original)
        if checks.empty or not checks.passed.astype(bool).all():
            raise ValueError(f"Cannot reuse hash-invalid original forecast: {run_id}")
    destination.mkdir()  # Never overwrite or reuse an earlier gate attempt.
    shutil.copy2(audit_path,destination/"fixture_gate_audit.json")
    inputs = destination / "inputs"
    shutil.copytree(base / "inputs", inputs)
    games = pd.read_csv(inputs / "games.csv")
    games = games[games.game_id.astype(str).isin(eligible)]
    games.to_csv(inputs / "games.csv", index=False)
    teams = set(games.away_team) | set(games.home_team)
    context=pd.read_csv(inputs/"team_context_schedule.csv")
    prior=(context.season.lt(int(meta["season"])) |
           (context.season.eq(int(meta["season"])) & context.week.lt(int(meta["week"]))))
    context[prior | context.game_id.astype(str).isin(eligible)].to_csv(inputs/"team_context_schedule.csv",index=False)
    # Scope every consumed current input, retaining PIT historical/team context bytes.
    for name in ["player_pregame_universe_features.csv", f"qb_status_{meta['season']}_w{int(meta['week']):02d}.csv",
                 f"weather_{meta['season']}_w{int(meta['week']):02d}.csv"]:
        path = inputs / name
        if path.exists():
            frame = pd.read_csv(path)
            frame[frame.game_id.astype(str).isin(eligible)].to_csv(path, index=False)
    for name in [f"weekly_roster_{meta['season']}.csv", f"injuries_{meta['season']}.csv", f"depth_charts_{meta['season']}.csv"]:
        path = inputs / name
        frame = pd.read_csv(path)
        frame[frame.team.isin(teams)].to_csv(path, index=False)
    eligibility = pd.read_csv(inputs / "game_eligibility.csv")
    by_game = {r["game_id"]: r for r in audit["fixtures"]}
    eligibility["eligibility_state"] = eligibility.game_id.map(lambda g: by_game[str(g)]["state"])
    eligibility["eligible_for_refresh"] = eligibility.game_id.astype(str).isin(eligible)
    eligibility["refresh_gate_status"] = eligibility.game_id.map(lambda g: by_game[str(g)]["status"])
    eligibility["refresh_gate_reason"] = eligibility.game_id.map(lambda g: "; ".join(by_game[str(g)]["reasons"]))
    eligibility.to_csv(inputs / "game_eligibility.csv", index=False)
    shutil.copy2(destination / "fixture_gate_audit.json", inputs / "fixture_gate_audit.json")
    manifest = pd.read_csv(base / "lock_manifest.csv")
    for index, row in manifest.iterrows():
        original = settings.root / row.locked_path
        target = inputs / original.name
        manifest.loc[index, "locked_path"] = str(target.relative_to(settings.root))
        manifest.loc[index, "sha256"] = sha256_file(target)
        # Aggregate provider failure is replaced only by certified per-fixture evidence.
        if row.source in CRITICAL_PROVIDERS:
            manifest.loc[index, ["source_state", "data_status", "freshness_status"]] = ["VALID", "READY", "READY"]
    for path in [inputs / "game_eligibility.csv", inputs / "fixture_gate_audit.json"]:
        manifest = pd.concat([manifest, pd.DataFrame([{"source": path.stem, "locked_path": str(path.relative_to(settings.root)),
            "sha256": sha256_file(path), "data_status": "READY", "critical": True}])], ignore_index=True)
    manifest.to_csv(destination / "lock_manifest.csv", index=False)
    meta = dict(meta, lock_id=lock_id, eligible_game_ids=sorted(eligible), cutoff_utc=now.isoformat(),
                created_at_utc=now.isoformat(), fixture_gate_version=GATE_VERSION, base_lock_id=meta["lock_id"],
                immutable_game_ids=sorted(f["game_id"] for f in audit["fixtures"] if f["state"] in {"FINAL","LIVE"}),
                retained_original_game_ids=sorted(omitted),
                blocked_game_ids=sorted(f["game_id"] for f in audit["fixtures"] if f["status"]=="BLOCKED" and f["state"] not in {"FINAL","LIVE"}),
                fixture_gate_audit_sha256=sha256_file(inputs / "fixture_gate_audit.json"))
    meta["manifest_sha256"] = sha256_file(destination / "lock_manifest.csv")
    for key, name in [("game_eligibility_sha256", "game_eligibility.csv"),
                      ("pregame_player_features_sha256", "player_pregame_universe_features.csv"),
                      ("team_context_schedule_sha256", "team_context_schedule.csv")]:
        meta[key] = sha256_file(inputs / name)
    # Unexpected global blockers are never converted into fixture eligibility.
    blockers = manifest[manifest.critical.fillna(False).astype(bool) & manifest.data_status.eq("BLOCKED")].source.tolist()
    meta.update(lock_status="BLOCKED" if blockers else "READY", production_blockers=blockers)
    _json(destination / "data_lock.json", meta)
    return destination


def recheck_inference_window(lock):
    meta = json.loads((lock / "data_lock.json").read_text())
    try:
        states, evidence = fetch_fixture_states(int(meta["season"]), int(meta["week"]))
    except Exception as exc:
        return False,{"checked_at_utc":datetime.now(timezone.utc).isoformat(),"error":str(exc),"fixtures":[]}
    now = pd.Timestamp(datetime.now(timezone.utc))
    selected = states[states.game_id.astype(str).isin(meta["eligible_game_ids"])].copy()
    locked = pd.read_csv(lock / "inputs/games.csv").set_index("game_id")
    changed = set(selected.game_id.astype(str)) != set(meta["eligible_game_ids"])
    for game in selected.itertuples():
        changed |= (game.eligibility_state != "PRE_KICKOFF" or pd.Timestamp(game.kickoff_utc) <= now
                    or pd.Timestamp(game.kickoff_utc) != pd.Timestamp(locked.loc[game.game_id, "kickoff_utc"]))
    return not changed, {"checked_at_utc": now.isoformat(), "evidence": evidence,
                         "fixtures": selected.to_dict("records")}


def validate_partial_certificate(bundle, meta=None):
    """Used by bundle validation and all official-selection/publication paths."""
    meta = meta or json.loads((bundle / "metadata.json").read_text())
    if meta.get("prediction_scope") != "UNPLAYED_ONLY":
        return []
    try:
        lock = json.loads((bundle / "data_lock.json").read_text())
        if not lock.get("fixture_gate_version") and pd.Timestamp(meta["created_at_utc"])<LEGACY_PARTIAL_BEFORE_UTC:
            # Preserve historical per-game selection without fabricating a new
            # certificate or modifying the old run, lock, predictions or freeze.
            required={"games.csv","players.csv","data_lock.json","game_prediction_manifest.csv","validation_report.csv","game_preview.json"}
            hashes=meta.get("file_hashes",{})
            if not required.issubset(hashes) or any(sha256_file(bundle/name)!=digest for name,digest in hashes.items()):
                raise ValueError("historical partial bundle is not hash-certified")
            checks=pd.read_csv(bundle/"validation_report.csv")
            if checks.empty or not checks.passed.astype(bool).all() or not {"injury_qb_source_gate","pregame_cutoff","qb_coverage"}.issubset(set(checks.check)):
                raise ValueError("historical partial validation contract did not pass")
            games=pd.read_csv(bundle/"games.csv")
            lineage=pd.read_csv(bundle/"game_prediction_manifest.csv")
            fresh=lineage[lineage.prediction_source_run_id.eq(meta["run_id"])]
            if (not games.game_id.is_unique or lineage.game_id.duplicated().any()
                or set(fresh.game_id.astype(str))!=set(games.game_id.astype(str))
                or not (pd.to_datetime(games.kickoff_utc,utc=True)>pd.Timestamp(meta["cutoff_utc"])).all()
                or meta.get("run_status")!="OFFICIAL_PRODUCTION" or meta.get("validation_result")!="PASS"
                or meta.get("deployment_eligible") is not True):
                raise ValueError("historical partial lineage/eligibility is not certified")
            return []
        audit_path = bundle / "fixture_gate_audit.json"
        audit = json.loads(audit_path.read_text())
        if lock.get("fixture_gate_version") != GATE_VERSION or sha256_file(audit_path) != lock.get("fixture_gate_audit_sha256"):
            raise ValueError("missing or invalid fixture gate certificate")
        games = pd.read_csv(bundle / "games.csv")
        ids = set(games.game_id.astype(str))
        if not ids or ids != set(audit["eligible_game_ids"]) or ids != set(lock["eligible_game_ids"]):
            raise ValueError("predictions differ from certified fixture subset")
        if audit.get("version")!=GATE_VERSION or ids!={str(f["game_id"]) for f in audit["fixtures"] if f["status"]=="ELIGIBLE" and f["state"]=="PRE_KICKOFF"}:
            raise ValueError("gate audit eligibility differs from predictions")
        lineage=pd.read_csv(bundle/"game_prediction_manifest.csv")
        all_ids={str(f["game_id"]) for f in audit["fixtures"]}
        if len(all_ids)!=len(audit["fixtures"]) or lineage.game_id.astype(str).duplicated().any() or set(lineage.game_id.astype(str))!=all_ids:
            raise ValueError("lineage does not cover the audited fixture set exactly once")
        fresh=lineage[~lineage.prediction_reused_immutable.astype(str).str.lower().isin(["true","1"])]
        if set(fresh.game_id.astype(str))!=ids or not fresh.prediction_source_run_id.eq(meta["run_id"]).all():
            raise ValueError("lineage does not preserve skipped original forecasts")
        checks = json.loads((bundle / "fixture_runtime_checks.json").read_text())
        if len(checks) != 2:
            raise ValueError("missing before/after inference state checks")
        previous=pd.Timestamp(audit["assessed_at_utc"])
        for check in checks:
            checked=pd.Timestamp(check["checked_at_utc"])
            if checked<previous:
                raise ValueError("runtime state checks are out of order")
            previous=checked
            rows = check["fixtures"]
            if {str(r["game_id"]) for r in rows} != ids or len(rows) != len(ids):
                raise ValueError("runtime state check scope differs")
            if any(r["eligibility_state"] != "PRE_KICKOFF" or pd.Timestamp(r["kickoff_utc"]) <= pd.Timestamp(check["checked_at_utc"]) for r in rows):
                raise ValueError("runtime state check is not pre-kickoff")
        for name in ["fixture_gate_audit.json", "fixture_runtime_checks.json", "data_lock.json", "game_prediction_manifest.csv"]:
            if sha256_file(bundle / name) != meta.get("file_hashes", {}).get(name):
                raise ValueError(f"uncertified partial artifact: {name}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f"Partial fixture certification failed: {exc}"]
    return []


def require_certified_partial_frontend(settings, source):
    """Bind publishable merged JSON to each referenced partial prediction bundle."""
    current=json.loads((Path(source)/"data/current_week.json").read_text())
    meta=current.get("metadata",{})
    if meta.get("fixture_revision_contract"):
        from nfl2026.frontend.fixture_revisions import validate_revision_week
        from nfl2026.production.fixture_revisions import FixtureRevisionService
        validate_revision_week(FixtureRevisionService(settings), current)
        # The joint revision verifier has checked the original partial/full
        # sources and every approval. Revision IDs are not legacy run folders.
        return
    refs=set(meta.get("selected_prediction_run_ids",[]))
    if meta.get("run_id"):refs.add(meta["run_id"])
    refs.update(game.get("prediction_source_run_id") or game.get("prediction_run_id") for game in current.get("games",[]))
    refs.discard(None)
    for ref in refs:
        parent=settings.prediction_root/str(meta.get("season"))/f"week_{int(meta.get('week',0)):02d}"
        bundle=parent/str(ref)
        if not bundle.resolve().is_relative_to(parent.resolve()):
            raise ValueError("Invalid prediction source reference")
        if not (bundle/"metadata.json").is_file():
            if meta.get("prediction_scope")=="UNPLAYED_ONLY":
                raise ValueError("Partial publication source bundle is missing")
            continue  # Existing full-week deployment contract remains unchanged.
        errors=validate_partial_certificate(bundle)
        if errors:raise ValueError("; ".join(errors))
    if meta.get("prediction_scope")=="UNPLAYED_ONLY" and not refs:
        raise ValueError("Partial publication lacks prediction source references")
