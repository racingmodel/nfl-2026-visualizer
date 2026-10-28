"""Opt-in revision-aware frontend data; original forecast and audit stay visible.

Exporting a local data artifact is separate from normal frontend release review,
deployment approval and publication. Existing public bytes are never changed.
"""
from copy import deepcopy
from pathlib import Path

from nfl2026.production.fixture_revisions import CONTRACT, canonical, digest, load


def compose_revision_week(service, payload, *, through_commit=None):
    meta = payload["metadata"]; season, week = int(meta["season"]), int(meta["week"])
    selected = service.selection(season, week, through_commit)
    if not selected:
        return deepcopy(payload)
    if meta.get("fixture_revision_contract"):
        raise ValueError("Use the immutable original payload, not an already revised payload")
    ids = [g["game_id"] for g in payload["games"]]
    if len(ids) != len(set(ids)) or set(selected)-set(ids):
        raise ValueError("Revision/frontend fixture population differs")
    result = deepcopy(payload)
    for index, original in enumerate(payload["games"]):
        gid = original["game_id"]
        if gid not in selected:
            continue
        trace = selected[gid]; candidate_path = service.settings.root/trace["candidate_path"]
        candidate = service.candidate(candidate_path)
        entry = candidate["fixtures"][gid]
        request = service.settings.root/candidate["request_path"]
        expected = next(g for g in load(request/"original_week.json")["games"] if g["game_id"] == gid)
        if any(original.get(k) != v for k,v in expected.items()):
            raise ValueError("Original forecast differs from the approved revision parent")
        pair = load(candidate_path/f"fixtures/{gid}.json")
        revised = deepcopy(pair["display"])
        for field in ["actual_home_score", "actual_away_score", "result_status", "result_source"]:
            if field in original:revised[field] = original[field]
        audit = []
        for commit_path in sorted((service.root(season, week)/"commits").glob("commit-*")):
            commit = load(commit_path/"commit.json")
            earlier = service.candidate(service.settings.root/commit["candidate_path"])
            if gid in earlier["fixtures"]:
                item = earlier["fixtures"][gid]
                approval_path = service.settings.root/commit["approval_path"]
                audit.append({"revision_id":item["revision_id"], "parent_run_id":item["parent_run_id"], "parent_revision_id":item["parent_revision_id"],
                              "request_id":earlier["request_id"], "candidate_id":earlier["candidate_id"], "reason":earlier["reason"],
                              "source_cutoff_utc":earlier["source_cutoff_utc"], "created_at_utc":earlier["created_at_utc"], "approved_at_utc":commit["committed_at_utc"],
                              "feature_hashes":earlier["feature_hashes"], "inference_feature_hashes":earlier["inference_feature_hashes"],
                              "source_timestamps":earlier["source_timestamps"], "model_ids":earlier["model_ids"],
                              "joint_forecast_sha256":item["joint_forecast_sha256"], "change_summary":item["change_summary"],
                              "approval_id":load(approval_path/"approval.json")["approval_id"]})
            if commit_path.name == through_commit or earlier["candidate_id"] == candidate["candidate_id"]:break
        revised.update(prediction_run_id=trace["revision_id"], prediction_source_run_id=trace["revision_id"],
                       prediction_cutoff_utc=candidate["source_cutoff_utc"], fixture_revision_id=trace["revision_id"],
                       team_source_revision_id=trace["revision_id"], player_source_revision_id=trace["revision_id"],
                       joint_forecast_sha256=entry["joint_forecast_sha256"], parent_run_id=entry["parent_run_id"],
                       original_forecast=deepcopy(original), fixture_revision_audit=audit,
                       fixture_revision_state="SIMULATED" if service.simulation else "APPROVED_LOCAL_REVISION")
        result["games"][index] = revised
    commits = sorted((service.root(season, week)/"commits").glob("commit-*"))
    commit_id = through_commit or commits[-1].name
    result["metadata"].update(fixture_revision_contract=CONTRACT, fixture_revision_commit_id=commit_id,
                               fixture_revision_selection_sha256=digest(selected), fixture_revision_simulation=service.simulation,
                               fixture_revision_original_metadata=deepcopy(meta))
    if service.simulation:
        result["metadata"].update(run_status="DRY_RUN", deployment_eligible=False)
    for old, new in zip(payload["games"], result["games"]):
        if old["game_id"] not in selected and canonical(old) != canonical(new):
            raise ValueError("Untouched fixture hash changed")
    return result


def validate_revision_week(service, payload, *, allow_simulation=False):
    from nfl2026.phase7.preview import verify_authoritative_bundle
    meta = payload["metadata"]
    if meta.get("fixture_revision_contract") != CONTRACT or (service.simulation and not allow_simulation):
        raise ValueError("Unsupported/simulated fixture revision publication contract")
    if meta.get("fixture_revision_simulation") is not service.simulation:
        raise ValueError("Simulation mode differs from revision evidence")
    selection = service.selection(int(meta["season"]), int(meta["week"]), meta["fixture_revision_commit_id"])
    if digest(selection) != meta["fixture_revision_selection_sha256"]:
        raise ValueError("Frontend fixture selection hash mismatch")
    base = {"metadata":deepcopy(meta["fixture_revision_original_metadata"]), "games":[]}
    for game in payload["games"]:
        if game["game_id"] in selection:
            if game.get("team_source_revision_id") != game.get("player_source_revision_id") or game.get("fixture_revision_id") != selection[game["game_id"]]["revision_id"]:
                raise ValueError("New player forecasts cannot use older team scores")
            base["games"].append(deepcopy(game["original_forecast"]))
        else:
            base["games"].append(deepcopy(game))
    # The original verifier remains strict, including each original source lock.
    audited = verify_authoritative_bundle(service.settings, base)
    expected = compose_revision_week(service, base, through_commit=meta["fixture_revision_commit_id"])
    if canonical(expected) != canonical(payload):
        raise ValueError("Frontend differs from the exact approved joint fixture revision")
    return audited


def export_revision_week(service, payload, destination):
    from nfl2026.production.fixture_revisions import seal_directory
    destination = Path(destination).resolve()
    for relative in ["site", "frontend/dist", "frontend/public", "frontend/releases"]:
        if destination.is_relative_to((service.settings.root/relative).resolve()):
            raise ValueError("Revision export cannot write a public/release destination")
    revised = compose_revision_week(service, payload)
    if revised["metadata"].get("fixture_revision_contract"):
        validate_revision_week(service, revised, allow_simulation=service.simulation)
    return seal_directory(destination, {"data/current_week.json":revised,
        "data/value.json":{"metadata":{"status":"MARKET_DATA_UNAVAILABLE","reason":"Fixture revisions require separately compatible value calculations; none were requested"},"values":[],"market_game_context":{},"all_quote_count":0},
        "export_receipt.json":{
        "schema":CONTRACT,"simulation":service.simulation,"publication":False,"normal_release_review_required":True,
        "current_week_sha256":digest(revised)}})
