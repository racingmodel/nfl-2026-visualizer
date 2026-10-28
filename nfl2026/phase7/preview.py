"""Build a local-only static preview; published/frontend-dist bytes never change."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import pandas as pd

from nfl2026.utilities.io import sha256_file
from nfl2026.phase7.common import json_write


def verify_authoritative_bundle(settings, bundle):
    """Cross-check display values against their original immutable per-game run."""
    if bundle.get("metadata",{}).get("fixture_revision_contract"):
        from nfl2026.frontend.fixture_revisions import validate_revision_week
        from nfl2026.production.fixture_revisions import FixtureRevisionService
        return validate_revision_week(FixtureRevisionService(settings), bundle)
    from nfl2026.production.core import verify_lock
    from nfl2026.frontend.lineage import PARTIAL_LINEAGE_FIELDS, certified_partial_lineage
    lineage = certified_partial_lineage(settings, bundle["metadata"])
    trace_fields = set(PARTIAL_LINEAGE_FIELDS)
    ids = [str(game["game_id"]) for game in bundle["games"]]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Frontend has empty or duplicate fixture IDs")
    if lineage:
        if len(bundle["games"]) != len(lineage) or {g["game_id"] for g in bundle["games"]} != set(lineage):
            raise ValueError("Frontend fixture population differs from certified partial lineage")
    cached, metadata_by_run, audited = {}, {}, []
    for game in bundle["games"]:
        run_id = game.get("prediction_source_run_id") or game.get("prediction_run_id") or bundle["metadata"]["run_id"]
        if run_id not in cached:
            run = settings.prediction_root / f"{bundle['metadata']['season']}/week_{int(bundle['metadata']['week']):02d}" / run_id
            meta = json.loads((run / "metadata.json").read_text())
            if meta.get('run_id')!=run_id or meta.get('run_status')!='OFFICIAL_PRODUCTION' or not meta.get('deployment_eligible'):
                raise ValueError("Preview source is not an official production forecast")
            if meta.get('prediction_scope') == 'UNPLAYED_ONLY':
                from nfl2026.production.partial_gate import validate_partial_certificate
                if validate_partial_certificate(run,meta):
                    raise ValueError('Preview source partial certification failed')
            metadata_by_run[run_id]=meta
            for name, digest in meta["file_hashes"].items():
                if sha256_file(run / name) != digest:
                    raise ValueError("Preview source forecast integrity failed")
            lock = settings.root / f"locks/{meta['season']}/week_{meta['week']:02d}" / meta["lock_id"]
            if verify_lock(lock):
                raise ValueError("Preview source lock integrity failed")
            cached[run_id] = {g["game_id"]: g for g in json.loads((run / "game_preview.json").read_text())["games"]}
        original = cached[run_id][game["game_id"]]
        if pd.Timestamp(game.get('prediction_cutoff_utc'))!=pd.Timestamp(metadata_by_run[run_id]['cutoff_utc']):
            raise ValueError("Displayed forecast cutoff differs from original immutable run")
        for field in original:
            if game[field] != original[field]:
                raise ValueError(f"Preview diverges from original forecast: {game['game_id']}/{field}")
        allowed=set(original)|{"prediction_run_id","prediction_source_run_id","prediction_cutoff_utc",
            "result_status","actual_home_score","actual_away_score","result_source"}
        if lineage:
            trace = lineage[game["game_id"]]
            if run_id != str(trace["prediction_source_run_id"]):
                raise ValueError("Frontend forecast source differs from certified partial lineage")
            for field in trace_fields:
                if field not in game or game[field] != trace[field] or type(game[field]) is not type(trace[field]):
                    raise ValueError("Frontend partial lineage differs: " + field)
            allowed |= trace_fields
        if set(game)-allowed:
            raise ValueError("Unsupported/research fields in official frontend game contract")
        audited.append({"game_id": game["game_id"], "source_run_id": run_id, "forecast_fields_verified": True})
    return audited


def build_preview(settings) -> Path:
    destination = settings.root / "frontend/phase7_preview"
    source = settings.root / "frontend/dist"
    from nfl2026.frontend.lineage import materialize_partial_lineage
    bundle = json.loads((source / "data/current_week.json").read_text())
    original_current = bundle
    bundle = materialize_partial_lineage(settings, bundle)
    verified = verify_authoritative_bundle(settings, bundle)
    destination.mkdir(parents=True, exist_ok=True)
    # Existing pages, historical navigation, assets and data are retained.
    # New preview files never flow through the production builder implicitly.
    shutil.copytree(source, destination, dirs_exist_ok=True)
    if bundle != original_current:
        json_write(destination / "data/current_week.json", bundle)
    # Results are presentation-only, from completed cohorts already validated by
    # the lab. Never rewrite a forecast, player projection, original timestamp,
    # or official bundle. This repairs stale historical display without leakage.
    from nfl2026.frontend.performance import build_performance_presentation
    performance = build_performance_presentation(settings)
    evaluated_weeks = {int(c["week"]) for c in performance["cohorts"] if c["evaluated_games"] > 0}
    schedule = pd.read_csv(settings.root / "data/phase2_external/games.csv", low_memory=False)
    completed = schedule[schedule.season.eq(2026) & schedule.week.isin(evaluated_weeks)
                         & schedule.home_score.notna() & schedule.away_score.notna()]
    actuals = completed.set_index("game_id")
    for bundle_path in (destination / "data").glob("week_*.json"):
        bundle = json.loads(bundle_path.read_text())
        bundle = materialize_partial_lineage(settings, bundle)
        verify_authoritative_bundle(settings, bundle)
        for game in bundle.get("games", []):
            if game["game_id"] in actuals.index:
                result = actuals.loc[game["game_id"]]
                game.update({"result_status": "FINAL", "actual_home_score": int(result.home_score),
                             "actual_away_score": int(result.away_score),
                             "result_source": "validated completed-week nflverse ingestion; presentation only"})
        json_write(bundle_path, bundle)
    templates = settings.root / "frontend/phase7_src"
    for name in ("index.html", "performance.html", "players.html", "value.html", "model.html", "phase7.css", "phase7.js", "boards.js"):
        shutil.copyfile(templates / name, destination / name)
    json_write(destination / 'data/phase7_performance.json', performance)
    for source_name, public_name in (("phase7_lab.json", "phase7_lab.json"),):
        shutil.copyfile(settings.report_root / source_name, destination / "data" / public_name)
    metadata = json.loads((destination / "data/current_week.json").read_text())["metadata"]
    run = settings.prediction_root / f"{metadata['season']}/week_{int(metadata['week']):02d}" / metadata["run_id"]
    run_meta = json.loads((run / "metadata.json").read_text())
    states = pd.read_csv(settings.root / f"locks/{metadata['season']}/week_{int(metadata['week']):02d}" / run_meta["lock_id"] / "lock_manifest.csv")
    json_write(destination / "data/phase7_source_health.json", {"forecast_sources": states[["source", "source_state", "data_status", "known_at_utc", "retrieved_at_utc", "quality_warnings"]].fillna("").to_dict("records"),
        "scope": "frozen input states at original prediction cutoff, not a new live refresh",
        "game_max_age_minutes": settings.market_featured_max_age_minutes,
        "prop_max_age_minutes": settings.market_player_prop_max_age_minutes})
    json_write(settings.report_root / "phase7_preview_manifest.json", {"status":"LOCAL_PREVIEW_NOT_PUBLISHED",
        "source_run_id": metadata["run_id"], "source_bundle_sha256": sha256_file(source / "data/current_week.json"),
        "files": {str(p.relative_to(destination)): sha256_file(p) for p in destination.rglob("*") if p.is_file()},
        "research_forecasts_displayed_as_official": False, "results_attached_to_historical_preview_only": True,
        "authoritative_forecasts_verified": verified,
        "serve_command": "python -m http.server 8768 --bind 127.0.0.1 --directory frontend/phase7_preview"})
    return destination / "index.html"
