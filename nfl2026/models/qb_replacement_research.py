"""Fail-closed, research-only quarterback provenance and lagged-quality helpers.

These functions do not change the registered team or player prediction paths.
Depth QB1 is an inferred expected starter, not a confirmed game-day starter.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


PIT_SAFE = "PIT_SAFE_INFERRED_QB1"
UNPROVEN = "UNPROVEN_PREGAME"
UNAVAILABLE = {"OUT", "IR", "PUP", "INACTIVE"}


def classify_expected_starter_source(*, source: str, known_at: object, kickoff: object) -> str:
    """Only timestamped depth snapshots strictly before kickoff qualify."""
    if source != "TIMESTAMPED_DEPTH_QB1":
        return UNPROVEN
    known = pd.to_datetime(known_at, utc=True, errors="coerce")
    start = pd.to_datetime(kickoff, utc=True, errors="coerce")
    if pd.isna(known) or pd.isna(start) or known >= start:
        return UNPROVEN
    return PIT_SAFE


def select_pregame_depth_qbs(fixtures: pd.DataFrame, depth: pd.DataFrame) -> pd.DataFrame:
    """Select each team's last timestamped pre-kickoff QB1 and QB2 snapshot."""
    required_fixture = {"game_id", "season", "week", "team", "kickoff_utc"}
    required_depth = {"dt", "team", "pos_abb", "pos_rank", "gsis_id", "player_name"}
    if not required_fixture.issubset(fixtures) or not required_depth.issubset(depth):
        raise ValueError("Missing fixture/depth provenance columns")
    if fixtures.duplicated(["game_id", "team"]).any():
        raise ValueError("Duplicate game/team fixture")
    qbs = depth[depth.pos_abb.eq("QB") & depth.gsis_id.notna()].copy()
    qbs["dt"] = pd.to_datetime(qbs.dt, utc=True, errors="coerce")
    qbs["pos_rank"] = pd.to_numeric(qbs.pos_rank, errors="coerce")
    qbs = qbs[qbs.dt.notna() & qbs.pos_rank.notna()]
    rows = []
    for game in fixtures.itertuples(index=False):
        kickoff = pd.to_datetime(game.kickoff_utc, utc=True, errors="coerce")
        earlier = qbs[(qbs.team.eq(game.team)) & (qbs.dt < kickoff)] if pd.notna(kickoff) else qbs.iloc[0:0]
        if earlier.empty:
            rows.append({"game_id": game.game_id, "season": game.season, "week": game.week,
                         "team": game.team, "kickoff_utc": game.kickoff_utc,
                         "starter_id": None, "starter_name": None, "backup_id": None,
                         "backup_name": None, "source_known_at_utc": None,
                         "source_class": UNPROVEN})
            continue
        snapshot = earlier.dt.max()
        ranked = earlier[earlier.dt.eq(snapshot)].sort_values(["pos_rank", "gsis_id"])
        ranked = ranked.drop_duplicates("gsis_id")
        starter = ranked.iloc[0]
        backups = ranked[ranked.gsis_id.ne(starter.gsis_id)]
        backup = backups.iloc[0] if len(backups) else None
        rows.append({"game_id": game.game_id, "season": game.season, "week": game.week,
                     "team": game.team, "kickoff_utc": game.kickoff_utc,
                     "starter_id": str(starter.gsis_id), "starter_name": str(starter.player_name),
                     "backup_id": str(backup.gsis_id) if backup is not None else None,
                     "backup_name": str(backup.player_name) if backup is not None else None,
                     "source_known_at_utc": snapshot.isoformat(),
                     "source_class": classify_expected_starter_source(
                         source="TIMESTAMPED_DEPTH_QB1", known_at=snapshot, kickoff=kickoff)})
    return pd.DataFrame(rows)


def lagged_qb_quality(
    history: pd.DataFrame, *, qb_id: str, season: int, week: int,
    prior_dropbacks: float = 200.0,
) -> dict[str, float]:
    """Empirical-Bayes EPA/dropback using only strictly earlier weeks.

    The prior is the earlier-week league rate. This is a *quality diagnostic*,
    not a learned team-point coefficient or a production replacement adjustment.
    """
    required = {"season", "week", "qb_id", "dropbacks", "epa_sum"}
    if not required.issubset(history):
        raise ValueError("Missing lagged quarterback outcome columns")
    if prior_dropbacks <= 0:
        raise ValueError("prior_dropbacks must be positive")
    prior = history[(history.season < season) | ((history.season == season) & (history.week < week))]
    n_league = float(prior.dropbacks.sum())
    league = float(prior.epa_sum.sum() / n_league) if n_league > 0 else 0.0
    player = prior[prior.qb_id.astype(str).eq(str(qb_id))]
    n = float(player.dropbacks.sum())
    epa = float(player.epa_sum.sum())
    shrunk = (epa + prior_dropbacks * league) / (n + prior_dropbacks)
    return {"dropbacks": n, "raw_epa_per_dropback": epa / n if n else np.nan,
            "league_prior_epa_per_dropback": league, "shrunk_epa_per_dropback": shrunk,
            "individual_weight": n / (n + prior_dropbacks)}


def starter_backup_delta(starter: dict[str, float], backup: dict[str, float] | None) -> float:
    if backup is None:
        return np.nan
    return float(starter["shrunk_epa_per_dropback"] - backup["shrunk_epa_per_dropback"])


def safe_replacement_context(
    *, source_state: str, injury_source_state: str, starter_status: str, replacement_confirmed: bool,
    replacement_source_state: str,
) -> bool:
    """Eligibility only; never infer a scoring effect or a replacement identity."""
    return (source_state == "VALID" and injury_source_state == "VALID"
            and starter_status.upper() in UNAVAILABLE
            and replacement_confirmed and replacement_source_state == "VALID")


def phase2_baseline_metrics(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {k: np.nan for k in ("team_mae", "team_rmse", "team_bias", "total_mae", "margin_mae")}
    he = frame.pred_home_score - frame.actual_home_score
    ae = frame.pred_away_score - frame.actual_away_score
    err = pd.concat([he, ae], ignore_index=True)
    return {"team_mae": float(err.abs().mean()), "team_rmse": float(np.sqrt((err ** 2).mean())),
            "team_bias": float(err.mean()), "total_mae": float((he + ae).abs().mean()),
            "margin_mae": float((he - ae).abs().mean())}
