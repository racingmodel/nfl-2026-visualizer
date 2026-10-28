"""Evidence-led player universes; a fixture-derived timestamp is not provenance."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd

from nfl2026.utilities.io import sha256_file

POSITIONS = {"QB", "RB", "FB", "WR", "TE"}
EVIDENCE_BASES = {"captured_pregame_snapshot", "independent_publication_timestamp"}


def schema_hash(columns) -> str:
    return hashlib.sha256(json.dumps(sorted(columns)).encode()).hexdigest()


def certified_pregame(row: dict, cutoff) -> bool:
    """Require an independently evidenced, hash-bound state available at cutoff.

    Effective dates, week labels and synthetic known_at values alone cannot pass.
    This certifies the supplied source observation, not all football eligibility.
    """
    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None:
        raise ValueError("An aware UTC cutoff is required")
    known = pd.to_datetime(row.get("known_at_utc"), utc=True, errors="coerce")
    retrieved = pd.to_datetime(row.get("retrieved_at_utc"), utc=True, errors="coerce")
    return bool(row.get("evidence_basis") in EVIDENCE_BASES
                and re.fullmatch(r"[0-9a-f]{64}", str(row.get("source_sha256", "")))
                and row.get("source_state") == "VALID"
                and not row.get("postgame_inferred", False)
                and pd.notna(known) and pd.notna(retrieved)
                and known <= cutoff and retrieved <= cutoff)


def reconstruct_universe(rosters: pd.DataFrame, games: pd.DataFrame, *,
                         source_sha256: str, evidence: dict | None = None) -> pd.DataFrame:
    """Retain claimed weekly membership, conflicts and uncertified rows explicitly.

    No box scores enter candidate selection. Status is a source claim, not a
    retrospectively certified game-day activation. Parallel depth slots are not
    inferred from roster order. Invalid IDs never join null-to-null.
    """
    required = {"season", "week", "team", "position", "status"}
    if not required.issubset(rosters):
        raise ValueError("Roster schema incomplete")
    r = rosters.copy().rename(columns={"gsis_id": "player_id", "full_name": "player_name"})
    if "player_id" not in r:
        raise ValueError("Roster has no GSIS identity")
    r = r[r.position.isin(POSITIONS) & r.player_id.notna() & r.player_id.astype(str).str.strip().ne("")]
    r["team"] = r.team.replace({"LAR": "LA", "WSH": "WAS", "OAK": "LV", "SD": "LAC"})
    if "game_type" in r:
        r = r[r.game_type.eq("REG")]
    g = games[games.game_type.eq("REG")].copy() if "game_type" in games else games.copy()
    teams = pd.concat([g.assign(team=g.home_team, opponent=g.away_team),
                       g.assign(team=g.away_team, opponent=g.home_team)], ignore_index=True)
    if teams.duplicated(["season", "week", "team"]).any():
        raise ValueError("Schedule has ambiguous weekly team association")
    keys = ["season", "week", "player_id"]
    r["membership_conflict"] = r.groupby(keys).team.transform("nunique").gt(1)
    r["status_conflict"] = r.groupby(keys + ["team"]).status.transform("nunique").gt(1)
    r = r.drop_duplicates(keys + ["team", "status", "position"])
    out = r.merge(teams[["season", "week", "team", "opponent", "game_id", "kickoff_utc"]],
                  on=["season", "week", "team"], validate="many_to_one")
    proof = {**(evidence or {}), "source_sha256": source_sha256}
    out["source_sha256"] = source_sha256
    out["known_at_utc"] = proof.get("known_at_utc", None)
    out["retrieved_at_utc"] = proof.get("retrieved_at_utc", None)
    out["evidence_basis"] = proof.get("evidence_basis", "WEEK_LABEL_ONLY")
    out["roster_observation_certified"] = [certified_pregame(proof, cutoff)
                                          for cutoff in out.kickoff_utc]
    out["eligibility_claim"] = np.where(out.status.eq("ACT"), "ACTIVE_IN_WEEKLY_SOURCE", "NONACTIVE_IN_WEEKLY_SOURCE")
    out["pregame_eligibility_certified"] = False  # needs complete independent availability/transaction evidence
    out["eligibility_evidence"] = np.where(out.roster_observation_certified,
        "PREGAME_ROSTER_OBSERVED_AVAILABILITY_NOT_CERTIFIED", "RETROSPECTIVE_WEEKLY_MEMBERSHIP")
    out["availability_status"] = "UNKNOWN"
    out["depth_role"] = "UNKNOWN"
    return out


def inventory(settings) -> pd.DataFrame:
    """Inventory real files, including absent source classes; never infer coverage."""
    rows = []
    patterns = {
        "roster": "data/phase2_external/weekly_roster_{season}.csv",
        "snaps": "data/phase2_external/snap_counts_{season}.csv",
        "participation": "data/phase2_external/participation_{season}.csv",
        "depth": "data/phase2_external/depth_charts_{season}.csv",
        "injuries": "data/phase2_external/injuries_{season}.csv",
        "transactions": "data/phase2_external/transactions_{season}.csv",
    }
    for season in range(2022, 2027):
        for source, pattern in patterns.items():
            path = settings.root / pattern.format(season=season)
            if not path.exists() and source in {"depth", "injuries", "transactions"}:
                legacy_name = {"depth": "depth_charts", "injuries": "injuries", "transactions": "transactions"}[source]
                legacy = settings.legacy_root / f"{season} Stats" / f"{legacy_name}_{season}.csv"
                if legacy.exists():
                    path = legacy
            frame = pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()
            timestamp_columns = [c for c in frame if c in {"known_at", "known_at_utc", "retrieved_at_utc", "dt", "date_modified", "effective_at"}]
            rows.append({"source": source, "season": season, "path": str(path.relative_to(settings.root)),
                "status": "PRESENT" if path.exists() else "UNAVAILABLE_LOCAL", "rows": len(frame),
                "sha256": sha256_file(path) if path.exists() else None,
                "timestamp_columns": "|".join(timestamp_columns), "schema_hash": schema_hash(frame.columns),
                "earliest_week": pd.to_numeric(frame.get("week", pd.Series(dtype=float)), errors="coerce").min(),
                "latest_week": pd.to_numeric(frame.get("week", pd.Series(dtype=float)), errors="coerce").max(),
                "pit_class": "POSTGAME_ONLY" if source in {"snaps", "participation"} else "UNPROVEN_HISTORICAL_PUBLICATION",
                "restriction": "participation 2023+ released after postseason; not in-season historical predictor" if source == "participation" else "source-specific evidence required"})
    for source, relative in {"schedule": "artifacts/canonical/canonical_games.csv",
        "weekly_stats": "artifacts/canonical/player_weekly.csv", "PBP_player_aggregates": "artifacts/canonical/player_pbp_weekly.csv",
        "historical_membership": "artifacts/canonical/player_team_membership.csv",
        "team_features": "artifacts/phase2/team_advanced_matrix.csv",
        "player_features": "artifacts/phase2/player_pregame_universe_features.csv"}.items():
        path = settings.root / relative
        frame = pd.read_csv(path, low_memory=False)
        for season in range(2022, 2027):
            block = frame[frame.season.eq(season)] if "season" in frame else frame.iloc[:0]
            rows.append({"source": source, "season": season, "path": relative,
                "status": "PRESENT" if len(block) else "UNAVAILABLE_LOCAL", "rows": len(block),
                "sha256": sha256_file(path), "schema_hash": schema_hash(frame.columns),
                "pit_class": "SYNTHETIC_TIMESTAMP_NOT_PROOF" if source == "historical_membership" else "SOURCE_SPECIFIC",
                "restriction": "postgame targets usable only as strictly prior completed outcomes"})
    import pyarrow.parquet as parquet
    for season in range(2022, 2027):
        path = settings.legacy_root / f"{season} Stats" / f"pbp_{season}.parquet"
        rows.append({"source":"PBP", "season":season,"path":str(path.relative_to(settings.root)),
            "status":"PRESENT" if path.exists() else "UNAVAILABLE_LOCAL",
            "rows":parquet.ParquetFile(path).metadata.num_rows if path.exists() else 0,
            "sha256":sha256_file(path) if path.exists() else None, "pit_class":"POSTGAME_ONLY",
            "restriction":"completed prior games only; publication latency must be evidenced for expanded features"})
        for source in ("QB_starter_identity", "weather_observed"):
            rows.append({"source":source,"season":season,"path":"data/phase2_external/games.csv",
                "status":"RETROSPECTIVE_SCHEDULE_FIELDS", "pit_class":"RETROSPECTIVELY_INFERRED",
                "restriction":"schedule actual QB / observed wind/temp are not certified pregame starters or forecasts"})
    return pd.DataFrame(rows)


def advanced_source_audit(settings) -> None:
    """Profile actual acquired files; document investigated-but-unused sources."""
    rows = []
    for kind in ("passing", "rushing", "receiving"):
        files = sorted((settings.root / "data/research/phase7").glob(f"*/ngs_{kind}.csv.gz"))
        if not files:
            rows.append({"source":f"NGS_{kind}","status":"UNAVAILABLE_LOCAL"})
            continue
        path = files[-1]
        frame = pd.read_csv(path)
        for season in range(2022, 2027):
            part = frame[frame.season.eq(season) & frame.season_type.eq("REG") & frame.week.gt(0)]
            rows.append({"source":f"NGS_{kind}","season":season,"status":"RESEARCH_ONLY_RETRIEVED",
                "rows":len(part),"minimum_week":int(part.week.min()) if len(part) else None,
                "maximum_week":int(part.week.max()) if len(part) else None,"sha256":sha256_file(path),
                "schema_hash":schema_hash(frame.columns),"path":str(path.relative_to(settings.root)),
                "missing_player_id_fraction":float(part.player_gsis_id.isna().mean()) if len(part) else None,
                "numeric_missing_fraction":float(part.select_dtypes(include="number").isna().mean().mean()) if len(part) else None,
                "known_at_validity":"lagged only; historical publication/revision timing unproved",
                "selection_limit":"minimum attempt eligibility; missing players do not mean zero efficiency",
                "licence":"underlying NFL terms; not redistributed in frontend",
                "incremental_usefulness":"NOT_CERTIFIED; requires source-latency audit and separate fold ablation",
                "update_frequency":"nightly per nflreadr documentation"})
    rows.extend([
        {"source":"FTN public charting", "status":"INVESTIGATED_NOT_INGESTED", "season":"2022+",
         "licence":"CC-BY-SA 4.0; FTN Data via nflverse attribution required", "update_frequency":"6-hour release polling; charting typically within 48h",
         "known_at_validity":"current-game charting is postgame; future use requires lagged completion/publication evidence"},
        {"source":"FTN participation 2023+", "status":"EXISTING_LOCAL_NOT_PREGAME_USABLE", "licence":"respect underlying provider licence",
         "update_frequency":"after postseason; not updated in-season", "known_at_validity":"BLOCKED as recent in-season historical predictor"},
        {"source":"pressure / routes / box counts", "status":"NOT_IMPLEMENTED", "known_at_validity":"requires licensed reliable source, IDs, timing and incremental validation"},
        {"source":"opponent-adjusted efficiency", "status":"EXISTING_PRIOR_GAME_TEAM_FEATURES", "known_at_validity":"existing team feature lineage; no new fitted adjustment promoted"},
    ])
    pd.DataFrame(rows).to_csv(settings.report_root / "phase7_advanced_source_audit.csv", index=False)


def frozen_roster_evidence(settings) -> pd.DataFrame:
    """Recover real 2026 pregame source captures, separate from retrospective history.

    Certifies captured roster observations only. Transaction coverage warnings
    and absent independent game-day activation prevent universal eligibility certification.
    """
    parts = []
    for week in range(1, 7):
        directories = sorted((settings.artifact_root / f"official_freezes/2026/week_{week:02d}").glob("freeze-*"))
        if not directories:
            continue
        selection = pd.read_csv(directories[-1] / "official_predictions.csv")
        for run_id, group in selection.groupby("official_prediction_run_id"):
            run = settings.prediction_root / f"2026/week_{week:02d}" / run_id
            metadata = json.loads((run / "metadata.json").read_text())
            lock = settings.root / f"locks/2026/week_{week:02d}" / metadata["lock_id"]
            manifest = pd.read_csv(lock / "lock_manifest.csv")
            roster = manifest[manifest.source.eq("roster")]
            if roster.empty:
                continue
            row = roster.iloc[-1]
            source = settings.root / row.locked_path
            if not source.exists() or sha256_file(source) != row.sha256:
                raise ValueError("Frozen roster source hash mismatch")
            proof = {"source_sha256": row.sha256, "known_at_utc": row.get("known_at_utc"),
                "retrieved_at_utc": row.get("retrieved_at_utc"), "source_state": row.get("source_state", "UNKNOWN"),
                "evidence_basis": "captured_pregame_snapshot"}
            games = pd.read_csv(lock / "inputs/games.csv")
            games = games[games.game_id.isin(group.game_id)].copy()
            if "kickoff_utc" not in games:
                from nfl2026.data.canonical import _kickoff_utc
                games["kickoff_utc"] = _kickoff_utc(games)
            candidates = reconstruct_universe(pd.read_csv(source, low_memory=False), games,
                source_sha256=row.sha256, evidence=proof)
            cutoff = metadata.get("cutoff_utc", metadata["created_at_utc"])
            candidates["roster_observation_certified"] &= certified_pregame(proof, cutoff)
            candidates["source_run_id"] = run_id
            candidates["source_lock_id"] = metadata["lock_id"]
            candidates["prediction_cutoff_utc"] = cutoff
            candidates["source_warnings"] = str(row.get("quality_warnings", ""))
            parts.append(candidates)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def feature_registry(settings) -> pd.DataFrame:
    """Extend the existing 136-row audit, preserving distinctions it qualifies."""
    frame = pd.read_csv(settings.report_root / "five_week_feature_audit.csv")
    frame["feature_version"] = settings.phase2_feature_version
    frame["known_at_timestamp"] = None
    frame["known_at_rule"] = frame.known_at_rule.astype(str)
    frame["historical_availability"] = "2022-2025; source-level publication evidence required"
    frame["leakage_classification"] = "TEMPORAL_CALCULATION_ONLY"
    unproven = frame.feature.str.contains("qb|roof|weather|temperature|wind", case=False)
    frame.loc[unproven, "leakage_classification"] = "UNKNOWN_HISTORICAL_PROVENANCE"
    frame["promotion_evidence_complete"] = False
    frame["lineage_schema_version"] = "phase7-lineage-v1"
    import joblib
    from nfl2026.registry.core import load_registered_model
    contracts = {}
    for name, version in (("team_score_model", settings.phase2_model_version),
        ("player_td_model", settings.phase2_model_version), ("player_opportunity_model", settings.phase2_player_model_version)):
        path, meta = load_registered_model(settings.artifact_root / "registry/model_registry.csv", name, version, settings.phase2_feature_version)
        contracts[name] = {"features": set(joblib.load(path)["features"]), "sha256": meta["artifact_sha256"]}
    frame["actual_model_users"] = ["|".join(name for name, c in contracts.items() if feature in c["features"])
                                    for feature in frame.feature]
    frame["model_artifact_hashes"] = ["|".join(c["sha256"] for c in contracts.values() if feature in c["features"])
                                      for feature in frame.feature]
    return frame


NGS_URLS = {kind: f"https://github.com/nflverse/nflverse-data/releases/download/nextgen_stats/ngs_{kind}.csv.gz"
            for kind in ("passing", "rushing", "receiving")}


def download_ngs(destination: Path, opener=urlopen) -> list[dict]:
    """Public nflverse release assets only, isolated and immutable; no scraping.

    Retrieval now does not prove historical publication. Season summaries week=0
    are retained in raw data but forbidden by the lagged research adapter.
    """
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    for kind, url in NGS_URLS.items():
        try:
            with opener(url, timeout=45) as response:
                if getattr(response, "status", 200) != 200:
                    raise ValueError("NGS retrieval was not HTTP 200")
                data = response.read()
            from io import BytesIO
            frame = pd.read_csv(BytesIO(data), compression="gzip")
            if not {"season", "week", "player_gsis_id"}.issubset(frame):
                raise ValueError("Unexpected NGS identity schema")
            digest = hashlib.sha256(data).hexdigest()
            (destination / f"ngs_{kind}.csv.gz").write_bytes(data)
            records.append({"source": f"ngs_{kind}", "url": url, "state": "RETRIEVED_RESEARCH_ONLY",
                "sha256": digest, "rows": len(frame), "seasons": sorted(frame.season.unique().tolist()),
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "known_at_basis": "retrieval now; historical release evidence unavailable",
                "licence": "underlying NFL data terms; no redistribution in preview",
                "production_allowed": False})
        except (OSError, ValueError) as exc:
            records.append({"source": f"ngs_{kind}", "url": url, "state": "ERROR", "error": str(exc), "production_allowed": False})
    (destination / "manifest.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


def lag_ngs(weekly: pd.DataFrame, candidates: pd.DataFrame, value_columns: list[str]) -> pd.DataFrame:
    """Strict prior-week NGS aggregates, not contemporaneous or season summaries.

    Output is explicitly retrospective research until publication timing is
    evidenced. Removing week=0 prevents full-season totals leaking into folds.
    """
    source = weekly[weekly.week.gt(0)].copy()
    if "season_type" in source:
        source = source[source.season_type.eq("REG")]
    if source.duplicated(["season", "week", "player_gsis_id"]).any():
        raise ValueError("NGS weekly identity is ambiguous")
    parts = []
    for (season, week), group in candidates.groupby(["season", "week"]):
        prior = source[(source.season.lt(season)) | (source.season.eq(season) & source.week.lt(week))]
        stats = prior.sort_values(["season", "week"]).groupby("player_gsis_id").tail(8)
        stats = stats.groupby("player_gsis_id")[value_columns].mean().add_prefix("lag_ngs_")
        part = group.merge(stats, left_on="player_id", right_index=True, how="left", validate="many_to_one")
        part["ngs_provenance"] = "LAGGED_RESEARCH_PUBLICATION_UNPROVEN"
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else candidates.copy()
