"""Approval-bound, immutable fixture revisions; no publication side effects.

Lifecycle: prepare request -> human request approval -> two inference replays ->
human candidate approval -> atomic local selection commit. The production
constructor has no clock, schedule or inference bypass. Simulations must use a
separate workspace and are rejected by production readers.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import copy
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
import time
import uuid

import pandas as pd

from nfl2026.production.fixture_revision_validation import eligible_states, utc, validate_sources, validate_coherence
from nfl2026.utilities.io import sha256_file

CONTRACT = "approved-fixture-revision-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def preview_equal(field, left, right):
    if field == "kickoff_utc":return utc(left) == utc(right)
    # Only these non-numerical display fields use CSV blank vs JSON empty
    # string conventions. Numerical forecasts and injury states stay exact.
    if field in {"data_quality_warnings", "practice_status", "warnings", "depth_role", "depth_source"}:
        if left in (None, "") and right in (None, ""):return True
    return left == right


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,159}", value):
        raise ValueError("Unsafe revision identity")
    return value


def engine_identity():
    root = Path(__file__).resolve().parents[2]
    names = ["nfl2026/production/fixture_revisions.py", "nfl2026/production/fixture_revision_validation.py",
             "nfl2026/production/fixture_revision_inference.py", "nfl2026/production/core.py", "scripts/build_phase2.py",
             "nfl2026/features/qb_identity_archive.py", "nfl2026/features/qb_archive_certification.py",
             "nfl2026/models/team_advanced.py", "nfl2026/models/player_advanced.py"]
    return {"files": {name: sha256_file(root/name) for name in names}, "python": sys.version,
            "packages": {name: importlib.metadata.version(name) for name in ["numpy", "pandas", "scikit-learn", "joblib"]}}


def observe_schedule(season, week):
    from nfl2026.live.pipeline import ASSETS
    from nfl2026.live.providers.base import fetch_bytes
    from nfl2026.live.partial_week import classify_game_eligibility
    body = fetch_bytes(ASSETS["games.csv"], attempts=1, timeout_seconds=20)
    observed = datetime.now(timezone.utc)
    states = classify_game_eligibility(pd.read_csv(BytesIO(body), low_memory=False), season, week, observed)
    return states, {"source_url": ASSETS["games.csv"], "observed_at_utc": observed.isoformat(),
                    "sha256": hashlib.sha256(body).hexdigest(), "raw_bytes": body}


def seal_directory(path, documents, files=None):
    """Commit marker is written last; incomplete attempts never become readable."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    for name, value in documents.items():
        target = path/name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical(value))
    for name, body in (files or {}).items():
        target = path/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
    contents = {str(p.relative_to(path)): sha256_file(p) for p in path.rglob("*") if p.is_file()}
    (path/"manifest.json").write_bytes(canonical({"schema": CONTRACT, "files": contents}))
    return path


def verify_directory(path):
    path = Path(path)
    manifest = load(path/"manifest.json")
    if manifest.get("schema") != CONTRACT or not manifest.get("files"):
        raise ValueError("Incomplete fixture revision artifact")
    actual = {str(p.relative_to(path)) for p in path.rglob("*") if p.is_file() and p != path/"manifest.json"}
    if actual != set(manifest["files"]):
        raise ValueError("Revision artifact file population changed")
    for name, expected in manifest["files"].items():
        target = (path/name).resolve()
        if not target.is_relative_to(path.resolve()) or sha256_file(target) != expected:
            raise ValueError("Revision artifact hash mismatch: "+name)
    return manifest


class FixtureRevisionService:
    def __init__(self, settings, *, simulation=False, clock=None, state_reader=None, infer=None):
        if not simulation and any(v is not None for v in [clock, state_reader, infer]):
            raise ValueError("Production fixture revisions have no injected clock/source/inference bypass")
        if simulation:
            from nfl2026.config import load_settings
            if settings.root.resolve() == load_settings().root.resolve():
                raise ValueError("Simulation requires an isolated workspace; never the real project")
        self.settings = settings
        self.simulation = bool(simulation)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state_reader = state_reader or observe_schedule
        from nfl2026.production.fixture_revision_inference import infer_candidate
        self.infer = infer or infer_candidate

    def root(self, season, week):
        return self.settings.artifact_root/"fixture_revisions"/str(int(season))/f"week_{int(week):02d}"

    def _inside(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.settings.root.resolve()):
            raise ValueError("Revision input must be inside this workspace")
        return path

    def _check_mode(self, item):
        if item.get("simulation") is not self.simulation:
            raise ValueError("Simulation evidence cannot enter production fixture selection")

    def _state(self, season, week, ids, expected=None):
        states, evidence = self.state_reader(season, week)
        now = utc(self.clock())
        observed = utc(evidence["observed_at_utc"])
        if observed > now or now-observed > pd.Timedelta(minutes=2) or not evidence.get("sha256"):
            raise ValueError("Independent fixture observation is stale, future or unbound")
        required = {"game_id", "eligibility_state", "kickoff_utc", "away_team", "home_team"}
        if not required.issubset(states) or states.game_id.duplicated().any():
            raise ValueError("Independent fixture states missing/duplicated")
        evidence = dict(evidence)
        raw = evidence.pop("raw_bytes", None)
        if not self.simulation and (raw is None or hashlib.sha256(raw).hexdigest() != evidence["sha256"]):
            raise ValueError("Independent schedule raw hash missing/different")
        # Retain independently observed closed states even when a preflight
        # fails. A later regressed source response cannot reopen that fixture.
        observations = self.root(season, week)/"observations"
        held = set()
        for previous in observations.glob("observation-*"):
            verify_directory(previous); prior = load(previous/"observation.json"); self._check_mode(prior)
            if utc(prior["observed_at_utc"]) > now:raise ValueError("Fixture observation clock regressed; no backdating")
            held.update(str(g["game_id"]) for g in prior["fixtures"] if g["eligibility_state"] in {"LIVE", "FINAL"})
        observation = {"schema":CONTRACT,"simulation":self.simulation,"season":season,"week":week,
                       "observed_at_utc":observed.isoformat(),"evidence":evidence,"fixtures":rows(states)}
        path = observations/("observation-"+digest(observation)[:24])
        if path.exists():verify_directory(path)
        else:seal_directory(path,{"observation.json":observation},{"schedule.csv":raw} if raw else {})
        if held&set(ids):raise ValueError("Fixture previously observed LIVE/FINAL is CLOSED; regressed evidence cannot reopen it")
        selected = eligible_states(states, ids, now, expected)
        return {"checked_at_utc": now.isoformat(), "simulation": self.simulation, "evidence": evidence, "fixtures": rows(selected)}, raw

    def _original(self, season, week, run_id):
        from nfl2026.production.core import validate_bundle, verify_lock
        run_id = identifier(run_id)
        bundle = self.settings.prediction_root/str(season)/f"week_{week:02d}"/run_id
        meta = load(bundle/"metadata.json")
        if (meta.get("run_id"), meta.get("season"), meta.get("week")) != (run_id, season, week) or meta.get("run_status") != "OFFICIAL_PRODUCTION" or not meta.get("deployment_eligible") or meta.get("validation_result") != "PASS":
            raise ValueError("Existing original is not certified official production")
        required = {"games.csv", "players.csv", "game_preview.json", "data_lock.json", "validation_report.csv"}
        if not required.issubset(meta.get("file_hashes", {})):
            raise ValueError("Original prediction lacks immutable source identities")
        checks = validate_bundle(bundle)
        if checks.empty or not checks.passed.astype(bool).all():
            raise ValueError("Original prediction hash validation failed")
        stored_checks = pd.read_csv(bundle/"validation_report.csv")
        if stored_checks.empty or not stored_checks.passed.astype(str).str.lower().isin(["true", "1"]).all():
            raise ValueError("Original prediction validation did not pass")
        lock = self.settings.root/f"locks/{season}/week_{week:02d}"/identifier(meta["lock_id"])
        if verify_lock(lock) or sha256_file(bundle/"data_lock.json") != sha256_file(lock/"data_lock.json"):
            raise ValueError("Original source lock identity/integrity failed")
        games = pd.read_csv(bundle/"games.csv", low_memory=False)
        if (not (pd.to_datetime(games.kickoff_utc, utc=True) > utc(meta["cutoff_utc"])).all()
                or not (pd.to_datetime(games.kickoff_utc, utc=True) > utc(meta["created_at_utc"])).all()
                or utc(meta["created_at_utc"]) < utc(meta["cutoff_utc"])):
            raise ValueError("Original forecast was not pregame")
        preview = load(bundle/"game_preview.json")
        if {g["game_id"] for g in preview["games"]} != set(games.game_id) or len(preview["games"]) != len(games):
            raise ValueError("Original preview population differs")
        return bundle, meta, preview

    def _binding(self, lock):
        lock = self._inside(lock)
        return {str(p.relative_to(self.settings.root)): sha256_file(p) for p in lock.rglob("*") if p.is_file()}

    def _request(self, request):
        request = self._inside(request); verify_directory(request)
        data = load(request/"request.json"); self._check_mode(data)
        if data["request_id"] != request.name or digest({k:v for k,v in data.items() if k != "request_id"})[:24] != data["request_id"].removeprefix("request-"):
            raise ValueError("Request immutable identity differs")
        return data

    def _fresh_request(self, request, data):
        now = utc(self.clock())
        if not utc(data["created_at_utc"]) <= now < utc(data["expires_at_utc"]):
            raise ValueError("Replacement request authorisation window expired/not yet valid")
        if data["engine_identity"] != engine_identity():
            raise ValueError("Inference implementation/runtime changed; new request approval required")
        if data["simulation_draws"] != self.settings.simulation_draws or data["feature_version"] != self.settings.phase2_feature_version:
            raise ValueError("Approved simulation/feature settings changed")
        for name, expected in data["source_lock_files"].items():
            if sha256_file(self.settings.root/name) != expected:
                raise ValueError("Approved input/source changed; new request required")
        bundle, meta, _ = self._original(data["season"], data["week"], data["parent_run_id"])
        if sha256_file(bundle/"metadata.json") != data["parent_metadata_sha256"]:
            raise ValueError("Original prediction identity changed")
        current = self.selection(data["season"], data["week"])
        for gid in data["fixture_ids"]:
            if current.get(gid) != data["parent_revisions"].get(gid):
                raise ValueError("Stale parent fixture revision; new request/approval required")
        return now

    def prepare(self, parent_run_id, source_lock, fixture_ids, reason, expires_at, *, seed=None):
        source_lock = self._inside(source_lock)
        meta = load(source_lock/"data_lock.json"); season, week = int(meta["season"]), int(meta["week"])
        ids = sorted(identifier(str(g)) for g in fixture_ids)
        if not ids or len(ids) != len(set(ids)) or not str(reason).strip():
            raise ValueError("Unique explicit fixture IDs and a replacement reason are required")
        if seed is not None and int(seed) <= 0:
            raise ValueError("The existing scorer contract requires a positive explicit seed")
        bundle, original, preview = self._original(season, week, parent_run_id)
        all_games = {g["game_id"]: g for g in preview["games"]}
        if set(ids)-set(all_games):
            raise ValueError("Requested fixture lacks an existing original forecast")
        state, raw = self._state(season, week, ids, all_games)
        evidence = validate_sources(self.settings, source_lock, ids, self.clock(), all_games, original["model_ids"], original["feature_version"])
        now = utc(self.clock()); expires = utc(expires_at)
        cutoffs = {}
        for gid in ids:
            limits = [utc(all_games[gid]["kickoff_utc"])-pd.Timedelta(minutes=1)]
            for document in [meta, original]:
                if document.get("revision_not_after_utc"):limits.append(utc(document["revision_not_after_utc"]))
                if document.get("fixture_revision_cutoffs",{}).get(gid):limits.append(utc(document["fixture_revision_cutoffs"][gid]))
            cutoffs[gid] = min(limits).isoformat()
        if not now < expires <= min(utc(v) for v in cutoffs.values()) or expires-now > pd.Timedelta(hours=24):
            raise ValueError("Approval expiry must be bounded before every cutoff (maximum 24 hours)")
        selected = self.selection(season, week)
        data = {"schema": CONTRACT, "simulation": self.simulation, "season": season, "week": week,
                "fixture_ids": ids, "reason": str(reason).strip(), "created_at_utc": now.isoformat(), "expires_at_utc": expires.isoformat(),
                "parent_run_id": original["run_id"], "parent_metadata_sha256": sha256_file(bundle/"metadata.json"),
                "parent_revisions": {g:selected.get(g) for g in ids}, "source_lock": str(source_lock.relative_to(self.settings.root)),
                "effective_revision_cutoffs":cutoffs,
                "source_lock_files": self._binding(source_lock), "model_ids": original["model_ids"], "feature_version": original["feature_version"],
                "seed": self.settings.random_seed if seed is None else int(seed), "simulation_draws": self.settings.simulation_draws,
                "engine_identity": engine_identity(), "revision_scope": "TEAM_AND_PLAYERS", "source_certification": evidence}
        data["request_id"] = "request-"+digest(data)[:24]
        destination = self.root(season, week)/"requests"/data["request_id"]
        if destination.exists():
            self._request(destination); return destination
        return seal_directory(destination, {"request.json": data, "original_week.json": preview, "prepare_state.json": state}, {"schedule.csv": raw} if raw else {})

    def approve(self, subject, *, actor, statement, expires_at):
        """Record a HUMAN attestation, never derive approval from task intent.

        This local trust boundary is explicit operator consent, not a claim of
        cryptographic human authentication. Automation must not self-approve.
        """
        subject = self._inside(subject); verify_directory(subject)
        name = "request.json" if (subject/"request.json").exists() else "candidate.json"
        item = load(subject/name); self._check_mode(item)
        kind = "REQUEST" if name == "request.json" else "REVISION"
        identity = item["request_id"] if kind == "REQUEST" else item["candidate_id"]
        bound = sha256_file(subject/"manifest.json")
        required = f"APPROVE {kind} {identity} {bound}"
        if not actor.strip() or statement != required:
            raise ValueError("Explicit exact user approval statement required: "+required)
        now = utc(self.clock()); expires = utc(expires_at)
        limit = utc(item["expires_at_utc"])
        if not now < expires <= limit:
            raise ValueError("Approval expiry must be within the subject's bounded window")
        approval = {"schema": CONTRACT, "simulation": self.simulation, "kind": kind, "subject_id": identity,
                    "subject_manifest_sha256": bound, "actor": actor.strip(), "statement": statement,
                    "issued_at_utc": now.isoformat(), "expires_at_utc": expires.isoformat(), "source": "EXPLICIT_USER_ATTESTATION"}
        approval["approval_id"] = "approval-"+digest(approval)[:24]
        root = self.root(item["season"], item["week"])/"approvals"/approval["approval_id"]
        if not root.exists():seal_directory(root, {"approval.json": approval})
        return root

    def _approval(self, path, subject, kind, now):
        path = self._inside(path); verify_directory(path); approval = load(path/"approval.json"); self._check_mode(approval)
        name = "request.json" if kind == "REQUEST" else "candidate.json"
        item = load(subject/name); identity = item["request_id"] if kind == "REQUEST" else item["candidate_id"]
        if approval.get("kind") != kind or approval.get("subject_id") != identity or approval.get("subject_manifest_sha256") != sha256_file(subject/"manifest.json"):
            raise ValueError("Approval bound to a different request/revision")
        if approval.get("source") != "EXPLICIT_USER_ATTESTATION" or approval.get("statement") != f"APPROVE {kind} {identity} {sha256_file(subject/'manifest.json')}":
            raise ValueError("Missing explicit user approval attestation")
        if not str(approval.get("actor", "")).strip() or approval.get("approval_id") != "approval-"+digest({k:v for k,v in approval.items() if k != "approval_id"})[:24]:
            raise ValueError("User approval identity invalid")
        if not utc(approval["issued_at_utc"]) <= utc(now) < utc(approval["expires_at_utc"]) <= utc(item["expires_at_utc"]):
            raise ValueError("User authorisation expired/not yet valid")
        return approval

    def certify(self, request, approval):
        request = self._inside(request); data = self._request(request)
        destination = self.root(data["season"], data["week"])/"candidates"/("candidate-"+data["request_id"].removeprefix("request-"))
        if destination.exists():
            self.candidate(destination); return destination  # read-only idempotent history
        now = self._fresh_request(request, data)
        self._approval(approval, request, "REQUEST", now)
        all_games = {g["game_id"]:g for g in load(request/"original_week.json")["games"]}
        source_lock = self.settings.root/data["source_lock"]
        attempt = self.root(data["season"], data["week"])/"attempts"/(data["request_id"]+"-"+uuid.uuid4().hex[:12])
        attempt.mkdir(parents=True)
        try:
            runtime = []; bundles = []
            for replay in ["first", "replay"]:
                state, raw = self._state(data["season"], data["week"], data["fixture_ids"], all_games)
                runtime.append(state)
                if raw:(attempt/(replay+"-schedule-before.csv")).write_bytes(raw)
                certification = validate_sources(self.settings, source_lock, data["fixture_ids"], self.clock(), all_games, data["model_ids"], data["feature_version"])
                # A short private workspace also works on Windows installations
                # without long-path support. Audit attempts retain its identity.
                workspace = self.settings.root/"_fixture_revision_work"/attempt.name[-12:]/replay
                (attempt/(replay+"-workspace.json")).write_bytes(canonical({"workspace":str(workspace.relative_to(self.settings.root))}))
                bundle = self.infer(self.settings, source_lock, data["fixture_ids"], workspace, data["model_ids"], data["seed"], certification)
                bundles.append(Path(bundle))
                self._fresh_request(request, data); self._approval(approval, request, "REQUEST", self.clock())
                state, raw = self._state(data["season"], data["week"], data["fixture_ids"], all_games); runtime.append(state)
                if raw:(attempt/(replay+"-schedule-after.csv")).write_bytes(raw)
            hashes = [{name:sha256_file(b/name) for name in ["games.csv", "players.csv"]} for b in bundles]
            if hashes[0] != hashes[1]:
                raise ValueError("Reproducible inference failed; original retained")
            first = bundles[0]; games = pd.read_csv(first/"games.csv"); players = pd.read_csv(first/"players.csv")
            if load(first/"metadata.json")["model_ids"] != data["model_ids"]:
                raise ValueError("Inference did not use the approved models")
            coherence = validate_coherence(games, players, {g:all_games[g] for g in data["fixture_ids"]})
            inference_meta = load(first/"data_lock.json")
            inference_feature_hashes = inference_meta.get("exact_feature_archive",{}).get("source_sha256",data["source_certification"]["feature_hashes"])
            preview = {g["game_id"]:g for g in load(first/"game_preview.json")["games"]}
            parent_bundle = self.settings.prediction_root/f"{data['season']}/week_{data['week']:02d}"/data["parent_run_id"]
            original_team_rows = pd.read_csv(parent_bundle/"games.csv",low_memory=False)
            original_player_rows = pd.read_csv(parent_bundle/"players.csv",low_memory=False)
            fixture_documents = {}; fixture_entries = {}
            for gid in data["fixture_ids"]:
                game = rows(games[games.game_id.eq(gid)])[0]; population = rows(players[players.game_id.eq(gid)].sort_values(["team", "player_id"]))
                display = preview[gid]
                for key, value in game.items():
                    if key not in display:continue
                    # CSV retains an ISO string while the supported preview
                    # serializer emits UTC datetimes with Z. Compare the exact
                    # instant; numerical forecasts still require exact equality.
                    equal = preview_equal(key,display[key],value)
                    if not equal:raise ValueError("Preview/team prediction coherence failed: "+key)
                by_player = {p["player_id"]:p for p in population}
                for player in display["players"]:
                    expected_player = by_player.get(player["player_id"])
                    if expected_player is None or set(player) != set(expected_player) or any(not preview_equal(k,v,expected_player[k]) for k,v in player.items()):
                        raise ValueError("Preview/player prediction coherence failed")
                pair = {"game":game, "players":population, "display":display}
                revision_id = "revision-"+digest({"request_id":data["request_id"], "fixture":pair})[:24]
                prior = data["parent_revisions"][gid]
                if prior:
                    previous = load(self.settings.root/prior["candidate_path"]/f"fixtures/{gid}.json")
                else:
                    previous = {"game":rows(original_team_rows[original_team_rows.game_id.eq(gid)])[0],
                                "players":rows(original_player_rows[original_player_rows.game_id.eq(gid)])}
                previous_players = {p["player_id"]:p for p in previous["players"]}
                changed_players = []
                for pid in sorted(set(previous_players)&set(by_player)):
                    changes = {k:{"previous":previous_players[pid].get(k),"revised":v} for k,v in by_player[pid].items()
                               if not preview_equal(k,previous_players[pid].get(k),v)}
                    if changes:changed_players.append({"player_id":pid,"changes":changes})
                fixture_documents[f"fixtures/{gid}.json"] = pair
                fixture_entries[gid] = {"revision_id":revision_id, "parent_run_id":data["parent_run_id"], "parent_revision_id":(data["parent_revisions"][gid] or {}).get("revision_id"),
                                        "joint_forecast_sha256":digest(pair), "team_sha256":digest(game), "players_sha256":digest(population),
                                        "original_forecast_sha256":digest(all_games[gid]), "change_summary":{
                                            "compared_with":prior["revision_id"] if prior else data["parent_run_id"],
                                            "team_fields":{k:{"previous":previous["game"].get(k),"revised":game.get(k)} for k in game if not preview_equal(k,previous["game"].get(k),game.get(k))},
                                            "players_added":sorted(set(by_player)-set(previous_players)),"players_removed":sorted(set(previous_players)-set(by_player)),
                                            "changed_players":changed_players,
                                            "original_display_players":len(all_games[gid].get("players",[])), "revised_display_players":len(display["players"])}}
            candidate = {"schema":CONTRACT, "simulation":self.simulation, "candidate_id":destination.name, "request_id":data["request_id"],
                         "season":data["season"], "week":data["week"], "created_at_utc":utc(self.clock()).isoformat(), "expires_at_utc":data["expires_at_utc"],
                         "request_path":str(request.relative_to(self.settings.root)), "request_sha256":sha256_file(request/"manifest.json"),
                         "request_approval_path":str(self._inside(approval).relative_to(self.settings.root)), "source_cutoff_utc":data["source_certification"]["source_cutoff_utc"],
                         "model_ids":data["model_ids"], "feature_version":data["feature_version"], "feature_hashes":data["source_certification"]["feature_hashes"],
                         "source_timestamps":data["source_certification"]["source_timestamps"], "effective_revision_cutoffs":data["effective_revision_cutoffs"],
                         "inference_feature_hashes":inference_feature_hashes,
                         "seed":data["seed"], "reason":data["reason"], "revision_scope":"TEAM_AND_PLAYERS", "fixtures":fixture_entries,
                         "reproducibility":{"status":"PASS","replay_output_hashes":hashes}, "coherence":coherence, "selection_status":"UNSELECTED_CANDIDATE", "publication":False}
            files = {path.name:path.read_bytes() for path in first.iterdir() if path.is_file()}
            return seal_directory(destination, {"candidate.json":candidate, "runtime_checks.json":runtime, **fixture_documents}, {"inference/"+k:v for k,v in files.items()})
        except Exception as exc:
            (attempt/"failure.json").write_bytes(canonical({"failed_at_utc":utc(self.clock()).isoformat(), "error":str(exc), "original_retained":True,"simulation":self.simulation}))
            raise

    def candidate(self, path):
        path = self._inside(path); verify_directory(path); item = load(path/"candidate.json"); self._check_mode(item)
        request = self.settings.root/item["request_path"]
        data = self._request(request)
        if item["request_sha256"] != sha256_file(request/"manifest.json") or item["model_ids"] != data["model_ids"] or item["revision_scope"] != "TEAM_AND_PLAYERS":
            raise ValueError("Candidate request/model/component identity mismatch")
        if item["reproducibility"]["status"] != "PASS" or len(item["reproducibility"]["replay_output_hashes"]) != 2 or item["reproducibility"]["replay_output_hashes"][0] != item["reproducibility"]["replay_output_hashes"][1]:
            raise ValueError("Candidate reproducibility evidence incomplete")
        for name, expected in item["reproducibility"]["replay_output_hashes"][0].items():
            if sha256_file(path/"inference"/name) != expected:raise ValueError("Candidate inference hash differs from replay")
        from nfl2026.production.core import validate_bundle
        checks = validate_bundle(path/"inference")
        if checks.empty or not checks.passed.astype(bool).all():raise ValueError("Candidate inference bundle integrity failed")
        for gid, entry in item["fixtures"].items():
            pair = load(path/f"fixtures/{identifier(gid)}.json")
            if (digest(pair),digest(pair["game"]),digest(pair["players"])) != (entry["joint_forecast_sha256"],entry["team_sha256"],entry["players_sha256"]):
                raise ValueError("Mixed team/player revision forbidden")
        return item

    def selection(self, season, week, through_commit=None):
        selected = {}
        for position, path in enumerate(sorted((self.root(season, week)/"commits").glob("commit-*")), 1):
            verify_directory(path); commit = load(path/"commit.json"); self._check_mode(commit)
            if commit["sequence"] != position or commit["before_selection_sha256"] != digest(selected):
                raise ValueError("Fixture selection commit history conflicted or incomplete")
            candidate_path = self.settings.root/commit["candidate_path"]
            item = self.candidate(candidate_path)
            if sha256_file(candidate_path/"manifest.json") != commit["candidate_manifest_sha256"]:
                raise ValueError("Selected candidate changed")
            self._approval(self.settings.root/commit["approval_path"], candidate_path, "REVISION", commit["committed_at_utc"])
            request = self.settings.root/item["request_path"]
            data = self._request(request)
            for name, expected in data["source_lock_files"].items():
                if sha256_file(self.settings.root/name) != expected:raise ValueError("Selected revision immutable source changed")
            self._approval(self.settings.root/item["request_approval_path"] , request, "REQUEST", commit["committed_at_utc"])
            state = load(path/"state_check.json")
            originals = {g["game_id"]:g for g in load(request/"original_week.json")["games"]}
            eligible_states(pd.DataFrame(state["fixtures"]), list(item["fixtures"]), commit["committed_at_utc"], originals)
            for gid, entry in item["fixtures"].items():
                parent = selected.get(gid)
                data = self._request(request)
                if parent != data["parent_revisions"][gid]:raise ValueError("Parent revision chain mismatch")
                selected[gid] = {"revision_id":entry["revision_id"], "candidate_id":item["candidate_id"], "candidate_path":commit["candidate_path"], "parent_run_id":entry["parent_run_id"], "approved_at_utc":commit["committed_at_utc"]}
            if commit["after_selection_sha256"] != digest(selected):raise ValueError("Fixture selection digest mismatch")
            if through_commit == path.name:return selected
        if through_commit is not None:raise ValueError("Unknown fixture revision commit snapshot")
        return selected

    @contextmanager
    def _mutex(self, root):
        root.mkdir(parents=True, exist_ok=True); path=root/".activation.lock"
        with path.open("x", encoding="utf-8") as stream:stream.write(str(uuid.uuid4()))
        try:yield
        finally:path.unlink()

    def activate(self, candidate, approval):
        candidate = self._inside(candidate); item = self.candidate(candidate)
        root = self.root(item["season"], item["week"])
        with self._mutex(root):
            commits = sorted((root/"commits").glob("commit-*"))
            for prior in commits:
                verify_directory(prior)
                if load(prior/"commit.json")["candidate_id"] == item["candidate_id"]:
                    self.selection(item["season"], item["week"]); return prior  # no new action after expiry/cutoff
            request = self.settings.root/item["request_path"]; data = self._request(request)
            now = self._fresh_request(request, data)
            self._approval(approval, candidate, "REVISION", now)
            self._approval(self.settings.root/item["request_approval_path"], request, "REQUEST", now)
            originals = {g["game_id"]:g for g in load(request/"original_week.json")["games"]}
            validate_sources(self.settings, self.settings.root/data["source_lock"], data["fixture_ids"], self.clock(), originals, data["model_ids"], data["feature_version"])
            state, raw = self._state(item["season"], item["week"], list(item["fixtures"]), originals)
            before = self.selection(item["season"], item["week"]); after = copy.deepcopy(before)
            committed = utc(self.clock()).isoformat()
            # Expiry/state is rechecked immediately before the atomic commit.
            self._approval(approval, candidate, "REVISION", committed); self._fresh_request(request, data)
            eligible_states(pd.DataFrame(state["fixtures"]), data["fixture_ids"], committed, originals)
            for gid, entry in item["fixtures"].items():
                after[gid] = {"revision_id":entry["revision_id"], "candidate_id":item["candidate_id"], "candidate_path":str(candidate.relative_to(self.settings.root)), "parent_run_id":entry["parent_run_id"], "approved_at_utc":committed}
            # Untouched selected fixture fingerprints cannot change in a commit.
            untouched = {g:digest(v) for g,v in before.items() if g not in item["fixtures"]}
            if any(digest(after[g]) != h for g,h in untouched.items()):raise ValueError("Untouched fixture selection changed")
            commit = {"schema":CONTRACT,"simulation":self.simulation,"sequence":len(commits)+1,"committed_at_utc":committed,
                      "candidate_id":item["candidate_id"],"candidate_path":str(candidate.relative_to(self.settings.root)),"candidate_manifest_sha256":sha256_file(candidate/"manifest.json"),
                      "approval_path":str(self._inside(approval).relative_to(self.settings.root)),"before_selection_sha256":digest(before),"after_selection_sha256":digest(after),
                      "untouched_selection_hashes":untouched,"publication":False}
            commit_id=f"commit-{len(commits)+1:08d}-"+digest(commit)[:16]
            staging = root/"pending"/(commit_id+"-"+uuid.uuid4().hex[:8])
            seal_directory(staging, {"commit.json":commit,"state_check.json":state}, {"schedule.csv":raw} if raw else {})
            destination = root/"commits"/commit_id; destination.parent.mkdir(parents=True, exist_ok=True)
            # A transient Windows scanner handle may briefly deny a directory
            # rename. Retry only that error, never overwrite/delete, and recheck
            # the expiry/cutoff immediately before every visibility attempt.
            for retry in range(4):
                instant = self.clock()
                self._approval(approval, candidate, "REVISION", instant)
                self._approval(self.settings.root/item["request_approval_path"], request, "REQUEST", instant)
                eligible_states(pd.DataFrame(state["fixtures"]), data["fixture_ids"], instant, originals)
                if utc(instant)-utc(state["evidence"]["observed_at_utc"]) > pd.Timedelta(minutes=2):raise ValueError("Commit state observation became stale")
                try:
                    staging.rename(destination)  # One same-volume visibility point.
                    break
                except PermissionError:
                    if retry == 3:raise
                    time.sleep(.05*(2**retry))
            return destination
