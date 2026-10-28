"""Run the existing scorer in an isolated candidate workspace.

No writes reach the project's prediction registry, weekly locks, freeze selector
or frontend. Both team and player outputs use the same exact locked inputs.
"""
from dataclasses import replace
from pathlib import Path
import json
import shutil

import pandas as pd

from nfl2026.production.core import generate_bundle, validate_bundle, verify_lock
from nfl2026.utilities.io import sha256_file


def infer_candidate(settings, source_lock, ids, workspace, model_ids, seed, certification):
    source_lock, workspace = Path(source_lock), Path(workspace)
    if certification.get("status") != "PASS" or set(certification.get("fixture_ids", [])) != set(ids):
        raise ValueError("Exact selected-fixture certification required before inference")
    if workspace.exists():
        raise FileExistsError("Inference workspace is immutable")
    workspace.mkdir(parents=True)
    source = json.loads((source_lock/"data_lock.json").read_text(encoding="utf-8"))
    season, week = int(source["season"]), int(source["week"])
    lock = workspace/f"locks/{season}/week_{week:02d}/fixture-inputs"
    shutil.copytree(source_lock, lock)
    inputs = lock/"inputs"
    games = pd.read_csv(inputs/"games.csv", low_memory=False)
    games = games[games.game_id.astype(str).isin(ids)].copy().sort_values("game_id")
    teams = set(games.home_team)|set(games.away_team)
    games.to_csv(inputs/"games.csv", index=False, lineterminator="\n")
    # Scores/results from an unselected Thursday game never enter the scorer.
    for path in inputs.glob("*.csv"):
        if path.name in {"games.csv", "historical_team_features.csv"}:
            continue
        frame = pd.read_csv(path, low_memory=False)
        if path.name == "team_context_schedule.csv":
            frame = frame[frame.game_id.astype(str).isin(ids)]
        elif "game_id" in frame and path.name not in {f"transactions_{season}.csv"}:
            frame = frame[frame.game_id.astype(str).isin(ids)]
        elif "team" in frame and path.name not in {f"transactions_{season}.csv"}:
            frame = frame[frame.team.isin(teams)]
        if path.name == f"weekly_roster_{season}.csv":
            frame = frame[frame.season.eq(season)&frame.week.eq(week)]
        frame.to_csv(path, index=False, lineterminator="\n")
    state_name = f"team_feature_state_{season}_w{week:02d}"
    if (inputs/(state_name+".json")).is_file():
        state_manifest_path = inputs/(state_name+".json")
        state_manifest = json.loads(state_manifest_path.read_text(encoding="utf-8"))
        state_manifest.update(derived_from_manifest_sha256=sha256_file(source_lock/"inputs"/(state_name+".json")),
                              derived_from_state_sha256=sha256_file(source_lock/"inputs"/(state_name+".csv")),
                              state_file_sha256=sha256_file(inputs/(state_name+".csv")), scope_fixture_ids=sorted(ids))
        state_manifest_path.write_text(json.dumps(state_manifest,indent=2,ensure_ascii=True),encoding="utf-8")
    manifest = pd.read_csv(lock/"lock_manifest.csv")
    for index, row in manifest.iterrows():
        path = inputs/Path(str(row.locked_path)).name
        if not path.is_file():
            raise ValueError("Locked dependency is not inside its inputs directory")
        manifest.loc[index, "locked_path"] = str(path.relative_to(workspace))
        manifest.loc[index, "sha256"] = sha256_file(path)
    manifest.to_csv(lock/"lock_manifest.csv", index=False, lineterminator="\n")
    meta = dict(source, lock_id="fixture-inputs", prediction_scope="FIXTURE_REVISION_CANDIDATE", eligible_game_ids=sorted(ids),
                run_scoped_replacement=False, manifest_sha256=sha256_file(lock/"lock_manifest.csv"))
    meta.update(lock_status="READY",production_blockers=[],scoped_source_certification=certification,
                original_aggregate_lock_status=source["lock_status"],original_aggregate_production_blockers=source.get("production_blockers",[]))
    for key, name in [("team_feature_state_sha256", f"team_feature_state_{season}_w{week:02d}.csv"),
                      ("team_feature_manifest_sha256", f"team_feature_state_{season}_w{week:02d}.json")]:
        if (inputs/name).is_file():
            meta[key] = sha256_file(inputs/name)
    archived = dict(meta.get("exact_feature_archive", {}))
    archived.update(status="READY", source_sha256={name: sha256_file(inputs/name) for name in ["historical_team_features.csv", "player_pregame_universe_features.csv"]})
    # Historical certificate/model/source validation has already passed in the
    # orchestrator. This is exact scoping of those certified source bytes.
    meta["exact_feature_archive"] = archived
    (lock/"data_lock.json").write_text(json.dumps(meta, indent=2, ensure_ascii=True), encoding="utf-8")
    errors = verify_lock(lock)
    if errors:
        raise ValueError("Scoped inference lock failed: "+str(errors))
    registry = workspace/"artifacts/registry"; registry.mkdir(parents=True)
    (registry/"model_registry.csv").write_bytes((settings.artifact_root/"registry/model_registry.csv").read_bytes())
    isolated = replace(settings, root=workspace, artifact_root=workspace/"artifacts", prediction_root=workspace/"predictions",
                       report_root=workspace/"reports", phase2_model_version=model_ids["team"]["version"],
                       phase2_player_model_version=model_ids["player"]["version"])
    isolated.report_root.mkdir()
    # DRY_RUN is an isolation status, not a source-gate waiver: the orchestrator
    # already applies stricter final-source/QB/history gates and runtime checks.
    bundle, _ = generate_bundle(isolated, lock, mode="dry-run", seed=seed, weekly_stage="FINAL", run_reason="Unapproved fixture-revision candidate")
    checks = validate_bundle(bundle)
    if checks.empty or not checks.passed.astype(bool).all():
        raise ValueError("Existing production scorer validation failed")
    metadata = json.loads((bundle/"metadata.json").read_text(encoding="utf-8"))
    if metadata["model_ids"] != model_ids or metadata["run_status"] != "DRY_RUN" or metadata["deployment_eligible"]:
        raise ValueError("Candidate model identity/isolation contract changed")
    return bundle
