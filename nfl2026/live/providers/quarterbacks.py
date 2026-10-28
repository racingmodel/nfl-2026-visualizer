from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from nfl2026.live.providers.base import ProviderResult, utc_iso


UNAVAILABLE = {"OUT", "IR", "PUP", "INACTIVE"}


def _injury_by_id(injuries: pd.DataFrame) -> dict[str, str]:
    if injuries.empty or "player_id" not in injuries:
        return {}
    rows = injuries[injuries.get("record_type", pd.Series(index=injuries.index, dtype=object)).eq("PLAYER_REPORT")]
    return dict(zip(rows.player_id.dropna().astype(str), rows.loc[rows.player_id.notna(), "game_status"].astype(str).str.upper()))


def load_quarterbacks(
    schedule: pd.DataFrame,
    roster: pd.DataFrame,
    depth: pd.DataFrame,
    week: int,
    retrieved_at: datetime,
    injuries: pd.DataFrame | None = None,
) -> ProviderResult:
    """Record schedule/depth expected starters without implying official confirmation."""
    active_rows = roster[(roster.week.eq(week)) & roster.status.eq("ACT") & roster.position.eq("QB")]
    active = set(active_rows.gsis_id.dropna().astype(str))
    active_by_team = {team: set(group.gsis_id.dropna().astype(str)) for team, group in active_rows.groupby("team")} if "team" in active_rows else {}
    injury_status = _injury_by_id(injuries if injuries is not None else pd.DataFrame())
    coverage = injuries[injuries.get("record_type", pd.Series(index=injuries.index, dtype=object)).eq("TEAM_COVERAGE")] if injuries is not None and len(injuries) else pd.DataFrame()
    injury_source_by_team = coverage.drop_duplicates("team", keep="last").set_index("team").source_state.to_dict() if len(coverage) and "source_state" in coverage else {}
    rows = []
    for game in schedule.itertuples(index=False):
        for side in ("away", "home"):
            team = getattr(game, f"{side}_team")
            player_id = str(getattr(game, f"{side}_qb_id"))
            player_name = getattr(game, f"{side}_qb_name")
            team_depth = depth[(depth.team.eq(team)) & depth.position.eq("QB")].copy() if len(depth) else pd.DataFrame()
            if len(team_depth):
                team_depth["depth_order"] = pd.to_numeric(team_depth.depth_order, errors="coerce")
                team_depth = team_depth.sort_values("depth_order")
            qb1 = team_depth[team_depth.depth_order.eq(1)].head(1)
            depth_id = str(qb1.iloc[0].player_id) if len(qb1) else ""
            roster_ok = player_id in active_by_team.get(team, active)
            superseded = bool(getattr(game, f'{side}_qb_depth_superseded', False))
            direct_starter = getattr(game, f'{side}_qb_resolution', '') == 'OFFICIAL_FIXTURE_STARTER'
            agreement = bool(depth_id and depth_id == player_id) or (superseded and direct_starter)
            status = "EXPECTED" if roster_ok else "UNCERTAIN"
            confidence = "HIGH" if roster_ok and agreement else "MEDIUM" if roster_ok else "LOW"
            player_health = injury_status.get(player_id, "UNKNOWN")
            if player_health in UNAVAILABLE:
                status, confidence = "UNCERTAIN", "LOW"
            selected_depth = team_depth[team_depth.player_id.astype(str).eq(player_id)] if len(team_depth) else pd.DataFrame()
            known = selected_depth.iloc[0].known_at_utc if agreement and len(selected_depth)==1 and "known_at_utc" in selected_depth else utc_iso(retrieved_at)
            # Active roster membership does not resolve conflicting starter
            # identities. Propagate the conflict into existing production gates.
            source_state = "VALID" if roster_ok and agreement else "SOURCE_MISSING"
            if roster_ok and not agreement:
                status = "UNCERTAIN"
            proof = json.loads(getattr(game, f"{side}_qb_evidence", "[]"))
            evidence_error = getattr(game, f"{side}_qb_evidence_error", "")
            confirmed = getattr(game, f"{side}_qb_confirmed", False) is True
            if proof:
                known = max([pd.Timestamp(known)] + [pd.Timestamp(e["known_at_utc"]) for e in proof]).isoformat()
            if roster_ok and agreement:
                depth_known = pd.to_datetime(known, utc=True, errors="coerce")
                depth_age = (pd.Timestamp(retrieved_at) - depth_known).total_seconds() / 3600 if pd.notna(depth_known) else float("inf")
                if depth_age < 0:
                    source_state = "ERROR"
                elif depth_age > 36:
                    source_state = "STALE"
                    confidence = "LOW"
            if evidence_error:
                source_state, status, confidence, confirmed = "ERROR", "UNCERTAIN", "LOW", False
            elif confirmed and roster_ok and agreement and player_health not in UNAVAILABLE:
                status = "CONFIRMED"
            backups = []
            for qb in team_depth.itertuples(index=False):
                qb_id = str(qb.player_id)
                if qb_id == player_id or qb_id not in active_by_team.get(team, active):
                    continue
                backups.append({"player_id": qb_id, "player_name": str(qb.player_name),
                                "depth_order": int(qb.depth_order) if pd.notna(qb.depth_order) else None,
                                "injury_status": injury_status.get(qb_id, "UNKNOWN")})
            rows.append({
                "game_id": game.game_id, "team": team, "qb_player_id": player_id,
                "qb_name": player_name, "status": status, "confidence": confidence,
                "source_state": source_state, "injury_source_state": injury_source_by_team.get(team, "SOURCE_MISSING"), "injury_status": player_health,
                "starter_confirmed": confirmed, "starter_resolution": getattr(game, f"{side}_qb_resolution", "SCHEDULE_DEPTH_INFERRED"),
                "original_qb_player_id": getattr(game, f"{side}_qb_original_id", player_id),
                "original_qb_name": getattr(game, f"{side}_qb_original_name", player_name),
                "official_evidence": json.dumps(proof, separators=(",", ":")),
                "reconciliation_error": evidence_error,
                "schedule_depth_agreement": agreement, "active_roster_match": roster_ok,
                "official_starter_supersedes_depth": superseded and direct_starter,
                "backup_hierarchy": json.dumps(backups, separators=(",", ":")),
                "source": "nflverse schedule projection + current depth + weekly roster",
                "known_at_utc": known, "retrieved_at_utc": utc_iso(retrieved_at),
            })
    records = pd.DataFrame(rows).sort_values("team").reset_index(drop=True)
    records["coverage_status"] = records.source_state
    coverage = records[["team", "game_id", "qb_player_id", "qb_name", "status", "confidence", "source_state",
                        "injury_source_state", "injury_status", "starter_confirmed", "schedule_depth_agreement", "active_roster_match",
                        "known_at_utc", "retrieved_at_utc"]].copy()
    good = int(records.source_state.eq("VALID").sum())
    status = "READY" if good == len(records) else "BLOCKED"
    warnings = [] if status == "READY" else [f"QB source/roster agreement {good}/{len(records)}"]
    known_at = pd.to_datetime(records.known_at_utc, utc=True, format="mixed").max().isoformat() if len(records) else utc_iso(retrieved_at)
    return ProviderResult(
        name="quarterbacks", records=records, coverage=coverage, status=status,
        source_url="nflverse schedule + depth_charts + weekly_rosters",
        retrieved_at_utc=utc_iso(retrieved_at), source_known_at_utc=known_at,
        source_known_at_basis="latest source row used; schedule projection is not official starter confirmation",
        reliability="INFERRED_EXPECTED_STARTER_NOT_CONFIRMED", warnings=warnings,
    )
