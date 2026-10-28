"""Exact feature-source archiving for new production locks only."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from nfl2026.utilities.io import sha256_file


def prepared_feature_sources(settings, season:int, week:int, cutoff:datetime,
                             require_model_contracts:bool=False) -> tuple[dict[str,Path],dict]:
    sources={
        "player_pregame_universe_features.csv":settings.artifact_root/"phase2/player_pregame_universe_features.csv",
        "historical_team_features.csv":settings.artifact_root/"phase2/team_advanced_matrix.csv",
    }
    absent=[name for name,path in sources.items() if not path.is_file()]
    if absent:
        return {},{"status":"BLOCKED","reason":"Exact prepared feature sources missing: "+", ".join(absent)}
    source_hashes={name:sha256_file(path) for name,path in sources.items()}
    player=pd.read_csv(sources["player_pregame_universe_features.csv"],low_memory=False)
    historical=pd.read_csv(sources["historical_team_features.csv"],low_memory=False)
    needed={"season","week","game_id","player_id","feature_version","prediction_cutoff_utc"}
    if not needed.issubset(player):raise ValueError("Prepared player feature source lacks its identity/PIT contract")
    block=player[pd.to_numeric(player.season,errors="coerce").eq(season)&pd.to_numeric(player.week,errors="coerce").eq(week)]
    if block.empty or block.duplicated(["game_id","player_id"]).any():raise ValueError("Prepared player target block missing/duplicated")
    if block.feature_version.astype(str).ne(settings.phase2_feature_version).any():raise ValueError("Prepared player feature version mismatch")
    known=pd.to_datetime(block.prediction_cutoff_utc,utc=True,errors="coerce")
    if known.isna().any() or known.gt(pd.Timestamp(cutoff)).any():raise ValueError("Prepared player feature cutoff invalid or future")
    if "effective_utc" in block:
        effective=pd.to_datetime(block.effective_utc,utc=True,errors="coerce")
        if effective.gt(pd.Timestamp(cutoff)).any():raise ValueError("Prepared player state has future effective timestamp")
    for column in ("source_latest_week","source_actuals_latest_week"):
        if column in block and pd.to_numeric(block[column],errors="coerce").ge(week).any():
            raise ValueError("Prepared player state contains target-week outcome history")
    # Copy original bytes for exact numerical replay, rather than round-tripping
    # floats through a newly generated CSV. Reject future blocks before copying.
    future=pd.to_numeric(player.season,errors="coerce").gt(season) | (
        pd.to_numeric(player.season,errors="coerce").eq(season)&pd.to_numeric(player.week,errors="coerce").gt(week))
    if future.any():raise ValueError("Feature archive contains unconsumed future player blocks")
    if not {"season","kickoff_utc"}.issubset(historical):raise ValueError("Historical team source lacks PIT keys")
    htime=pd.to_datetime(historical.kickoff_utc,utc=True,errors="coerce")
    if htime.isna().any() or htime.ge(pd.Timestamp(cutoff)).any() or pd.to_numeric(historical.season,errors="coerce").ge(season).any():
        raise ValueError("Historical team feature source contains current/future outcomes")
    contracts={}
    if require_model_contracts:
        from nfl2026.features.qb_identity_archive import require_identity_archive
        # Old frozen archives remain immutable/readable. New production locks
        # cannot certify the identity-free CSV that caused median-only QBs.
        import joblib
        from nfl2026.registry.core import load_registered_model
        registry=settings.artifact_root/"registry/model_registry.csv"
        for name,version,frame in (("team_score_model",settings.phase2_model_version,historical),
            ("player_opportunity_model",settings.phase2_player_model_version,block),
            ("player_td_model",settings.phase2_model_version,block)):
            path,row=load_registered_model(registry,name,version,settings.phase2_feature_version)
            required=joblib.load(path)["features"]
            missing=sorted(set(required)-set(frame))
            if missing:raise ValueError(f"Exact feature archive violates {name} schema: {missing}")
            contracts[name]={"version":version,"artifact_sha256":str(row["artifact_sha256"]),"feature_count":len(required)}
        require_identity_archive(historical)
        from nfl2026.features.qb_archive_certification import validate_identity_certificate
        contracts['historical_qb_identity']=validate_identity_certificate(
            historical,sources['historical_team_features.csv'],contracts['team_score_model']['artifact_sha256'],settings.root)
    if any(sha256_file(path)!=source_hashes[name] for name,path in sources.items()):
        raise ValueError("Prepared feature source changed during validation")
    info={"status":"READY","scope":"full original source bytes; production consumes requested season/week only",
        "target_player_rows":len(block),"feature_version":settings.phase2_feature_version,
        "source_row_counts":{"player_pregame_universe_features.csv":len(player),"historical_team_features.csv":len(historical)},
        "model_contracts":contracts,
        "source_sha256":source_hashes,
        "source_paths":{name:str(path.relative_to(settings.root)) for name,path in sources.items()}}
    return sources,info
