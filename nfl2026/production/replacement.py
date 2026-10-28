from __future__ import annotations

from typing import Any

import pandas as pd
from nfl2026.utilities.io import sha256_file


INACTIVE_ROSTER_STATES = {"IR", "RESERVE", "PUP", "INACTIVE", "RELEASED", "WAIVED", "PRACTICE_SQUAD"}
INACTIVE_INJURY_STATES = {"OUT", "IR", "PUP", "INACTIVE"}


def build_run_scoped_player_state(
    features: pd.DataFrame,
    roster: pd.DataFrame,
    injuries: pd.DataFrame,
    depth: pd.DataFrame,
    schedule: pd.DataFrame,
    *,
    season: int,
    week: int,
    feature_version: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Overlay current eligibility/annotation onto an immutable numerical feature block."""
    block = features[
        pd.to_numeric(features.season, errors="coerce").eq(season)
        & pd.to_numeric(features.week, errors="coerce").eq(week)
    ].copy().reset_index(drop=True)
    if block.empty:
        raise ValueError(f"No validated player feature rows for {season} Week {week}")
    if "feature_version" not in block or block.feature_version.astype(str).ne(feature_version).any():
        raise ValueError("Replacement player state has incompatible feature-version provenance")
    if "player_id" not in block or block.player_id.isna().any() or block.player_id.astype(str).eq("").any():
        raise ValueError("Replacement player state requires identified candidates")
    if "game_id" not in block or block.duplicated(["game_id", "player_id"]).any():
        raise ValueError("Replacement player state has missing or duplicate game/player rows")

    target_roster = roster[pd.to_numeric(roster.season, errors="coerce").eq(season)
        & pd.to_numeric(roster.week, errors="coerce").eq(week)].copy()
    if target_roster.empty or not {"gsis_id", "team", "status"}.issubset(target_roster.columns):
        raise ValueError("Current target-week roster state is missing required identity/status fields")
    target_roster = target_roster.dropna(subset=["gsis_id"]).copy()
    target_roster["gsis_id"] = target_roster.gsis_id.astype(str)
    if target_roster.gsis_id.duplicated().any():
        # A player may not be simultaneously current on multiple teams.
        ambiguous = target_roster[target_roster.gsis_id.duplicated(False)].gsis_id.unique().tolist()
        teams = target_roster[target_roster.gsis_id.isin(ambiguous)].groupby("gsis_id").team.nunique()
        if teams.gt(1).any():
            raise ValueError(f"Current roster has irreconcilable team assignments: {teams[teams.gt(1)].index.tolist()[:8]}")
        target_roster = target_roster.drop_duplicates("gsis_id", keep="last")

    target_games = schedule[pd.to_numeric(schedule.season, errors="coerce").eq(season)
        & pd.to_numeric(schedule.week, errors="coerce").eq(week)
        & schedule.game_type.astype(str).str.upper().eq("REG")].copy()
    if target_games.empty or target_games[["home_score", "away_score"]].notna().any().any():
        raise ValueError("Replacement player state requires an unplayed target-week schedule")
    if "kickoff_utc" not in target_games:
        local = pd.to_datetime(target_games.gameday.astype(str)+" "+target_games.gametime.fillna("13:00").astype(str), errors="coerce")
        target_games["kickoff_utc"] = local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    else:
        target_games["kickoff_utc"] = pd.to_datetime(target_games.kickoff_utc, utc=True, errors="coerce")
    if target_games.kickoff_utc.isna().any():
        raise ValueError("Replacement target schedule has an invalid kickoff")
    team_to_game: dict[str, tuple[str, str, str]] = {}
    for game in target_games.itertuples(index=False):
        if game.home_team in team_to_game or game.away_team in team_to_game:
            raise ValueError("A scheduled team maps to multiple target-week games")
        kickoff = pd.Timestamp(game.kickoff_utc).isoformat()
        team_to_game[str(game.home_team)] = (str(game.game_id), str(game.away_team), kickoff)
        team_to_game[str(game.away_team)] = (str(game.game_id), str(game.home_team), kickoff)

    old_eligibility = block.get("status_eligible", pd.Series(False, index=block.index)).astype(str).str.lower().isin(["true", "1"])
    block["old_status_eligible"] = old_eligibility
    block["live_roster_status"] = "SOURCE_MISSING"
    block["live_transaction_state"] = ""
    block["live_team"] = ""
    block["live_injury_status"] = "UNKNOWN"
    block["live_injury_source_status"] = "UNKNOWN"
    block["live_depth_order"] = pd.NA
    block["live_depth_role"] = ""
    block["live_depth_source"] = ""

    roster_by_id = target_roster.set_index("gsis_id")
    injury_rows = injuries.copy()
    if len(injury_rows):
        if "record_type" in injury_rows:
            injury_rows = injury_rows[injury_rows.record_type.astype(str).eq("PLAYER_REPORT")]
        id_col = "player_id" if "player_id" in injury_rows else "gsis_id" if "gsis_id" in injury_rows else None
        status_col = next((name for name in ("game_status", "normalized_status", "report_status") if name in injury_rows), None)
        if id_col and status_col:
            injury_rows = injury_rows.dropna(subset=[id_col]).drop_duplicates(id_col, keep="last").set_index(id_col)
        else:
            injury_rows = pd.DataFrame()

    depth_rows = depth.copy()
    if len(depth_rows):
        player_col = "player_id" if "player_id" in depth_rows else "gsis_id" if "gsis_id" in depth_rows else None
        if player_col:
            depth_rows[player_col] = depth_rows[player_col].astype(str)
            depth_keys=[player_col,"team"]+(['position'] if 'position' in depth_rows else [])
            depth_rows = depth_rows.drop_duplicates(depth_keys, keep="last").set_index(depth_keys)
        else:
            depth_rows = pd.DataFrame()

    original_team = block.team.astype(str).copy()
    new_eligible = pd.Series(False, index=block.index)
    for idx, row in block.iterrows():
        pid = str(row.player_id)
        if pid not in roster_by_id.index:
            continue
        current = roster_by_id.loc[pid]
        status = str(current.status).strip().upper()
        team = str(current.team)
        block.loc[idx, "live_roster_status"] = status
        current_tx = current.get("current_transaction_state", "")
        current_tx = "" if pd.isna(current_tx) else str(current_tx).strip().upper()
        block.loc[idx, "live_transaction_state"] = current_tx
        block.loc[idx, "live_team"] = team
        if team in team_to_game:
            game_id, opponent, kickoff = team_to_game[team]
            block.loc[idx, ["team", "opponent", "game_id", "kickoff_utc"]] = [team, opponent, game_id, kickoff]
        else:
            status = "ROSTER_TEAM_NOT_SCHEDULED"
            block.loc[idx, "live_roster_status"] = status
        injury_status = "UNKNOWN"
        if len(injury_rows) and pid in injury_rows.index:
            injury_row = injury_rows.loc[pid]
            status_col = next((name for name in ("game_status", "normalized_status", "report_status") if name in injury_row), None)
            if status_col:
                injury_status = str(injury_row.get(status_col, "UNKNOWN")).upper()
                block.loc[idx, "live_injury_source_status"] = injury_status
                transaction_state = current_tx
                transaction_at = pd.to_datetime(current.get("current_transaction_event_at_utc", pd.NaT), utc=True, errors="coerce")
                injury_at = pd.to_datetime(injury_row.get("known_at_utc", injury_row.get("retrieved_at_utc", pd.NaT)), utc=True, errors="coerce")
                if transaction_state == "ACTIVE" and injury_status in INACTIVE_INJURY_STATES and pd.notna(transaction_at) and (pd.isna(injury_at) or transaction_at > injury_at):
                    # A later authoritative activation supersedes an older
                    # reserve/inactive report, but absence of a newer health
                    # report remains UNKNOWN rather than being called healthy.
                    injury_status = "UNKNOWN"
        block.loc[idx, "live_injury_status"] = injury_status
        depth_position='FB' if row.position=='RB' and current.get('depth_chart_position')=='FB' else row.position
        depth_key=(pid,team,depth_position) if len(depth_rows) and depth_rows.index.nlevels==3 else (pid,team)
        if len(depth_rows) and depth_key in depth_rows.index:
            depth_row = depth_rows.loc[depth_key]
            for source_col, target_col in (("depth_order", "live_depth_order"), ("depth_role", "live_depth_role"), ("depth_source", "live_depth_source")):
                if source_col in depth_row:
                    block.loc[idx, target_col] = depth_row[source_col]
        eligible = status in {"ACT", "ACTIVE"} and current_tx not in INACTIVE_ROSTER_STATES and injury_status not in INACTIVE_INJURY_STATES and team in team_to_game
        new_eligible.loc[idx] = eligible

    block["status"] = block.live_roster_status
    block["status_eligible"] = new_eligible
    block["replacement_eligibility_state"] = new_eligible.map({True: "ACTIVE", False: "INACTIVE_OR_NOT_ON_CURRENT_ROSTER"})

    original_eligible_ids = set(block.loc[old_eligibility, "player_id"].astype(str))
    replacement_eligible_ids = set(block.loc[new_eligible, "player_id"].astype(str))
    removed = sorted(original_eligible_ids - replacement_eligible_ids)
    added = sorted(replacement_eligible_ids - original_eligible_ids)
    moved = block[original_team.ne(block.team.astype(str)) & new_eligible]
    unchanged_ids = original_eligible_ids & replacement_eligible_ids
    numerical_columns = [
        col for col in block.columns
        if col not in {"season", "week", "status_eligible", "player_id", "game_id"}
        and pd.api.types.is_numeric_dtype(block[col])
    ]
    baseline = features[
        pd.to_numeric(features.season, errors="coerce").eq(season)
        & pd.to_numeric(features.week, errors="coerce").eq(week)
        & features.player_id.astype(str).isin(unchanged_ids)
    ].set_index("player_id")
    current = block[block.player_id.astype(str).isin(unchanged_ids)].set_index("player_id")
    if baseline.index.has_duplicates or current.index.has_duplicates:
        raise ValueError("Numerical comparison requires unique player rows per game")
    numeric_differences = 0
    for col in numerical_columns:
        if col not in baseline or col not in current:
            continue
        left = pd.to_numeric(baseline[col], errors="coerce").sort_index()
        right = pd.to_numeric(current[col], errors="coerce").sort_index()
        aligned = left.index.intersection(right.index)
        unequal = ~(left.loc[aligned].eq(right.loc[aligned]) | (left.loc[aligned].isna() & right.loc[aligned].isna()))
        numeric_differences += int(unequal.sum())

    transaction = block.live_transaction_state.astype(str).str.upper()
    injury = block.live_injury_status.astype(str).str.upper()
    wrong_team = int((block.status_eligible & block.live_team.astype(str).ne(block.team.astype(str))).sum())
    summary = {
        "old_eligible_count": int(old_eligibility.sum()),
        "replacement_eligible_count": int(new_eligible.sum()),
        "players_removed": removed,
        "players_removed_details": block[block.player_id.astype(str).isin(removed)][["player_id", "player_name", "team", "live_roster_status", "live_transaction_state"]].drop_duplicates("player_id").to_dict("records"),
        "players_added": added,
        "players_added_details": block[block.player_id.astype(str).isin(added)][["player_id", "player_name", "team"]].drop_duplicates("player_id").to_dict("records"),
        "team_changes": [{"player_id": str(r.player_id), "player_name": str(r.player_name), "old_team": str(original_team.loc[i]), "new_team": str(r.team)} for i, r in block.loc[original_team.ne(block.team.astype(str))].iterrows()],
        "wrong_team_eligible_count": wrong_team,
        "ir_reserve_eligible_count": int((block.status_eligible & (transaction.isin({"IR", "RESERVE"}) | injury.isin({"IR", "PUP"}))).sum()),
        "released_waived_eligible_count": int((block.status_eligible & transaction.isin({"RELEASED", "WAIVED"})).sum()),
        "historical_numerical_feature_differences": numeric_differences,
        "old_feature_version": feature_version,
        "replacement_feature_version": feature_version,
        "eligible_player_ids": sorted(replacement_eligible_ids),
    }
    return block, summary


def compare_with_existing_run(old_players: pd.DataFrame, old_roster: pd.DataFrame, new_state: pd.DataFrame,
                              summary: dict[str, Any]) -> dict[str, Any]:
    """Add audit deltas against a frozen prediction without changing that run."""
    old = old_players.drop_duplicates("player_id", keep="last").copy()
    new_eligible = new_state[new_state.status_eligible.astype(str).str.lower().isin(["true", "1"])]
    old_ids = set(old.player_id.astype(str))
    new_ids = set(new_eligible.player_id.astype(str))
    old["player_id"] = old.player_id.astype(str)
    old_by = old.drop_duplicates("player_id", keep="last").set_index("player_id")
    new_state = new_state.drop_duplicates("player_id", keep="last").copy()
    new_state["player_id"] = new_state.player_id.astype(str)
    new_all_by = new_state.set_index("player_id")
    new_by = new_state[new_state.player_id.isin(new_ids)].set_index("player_id")
    common = old_ids & new_ids
    team_changes = []
    depth_changes = []
    injury_changes = []
    for pid in sorted(common):
        before = old_by.loc[pid]
        after = new_by.loc[pid]
        if str(before.get("team", "")) != str(after.get("team", "")):
            team_changes.append({"player_id": pid, "player_name": str(after.get("player_name", "")),
                                 "old_team": str(before.get("team", "")), "new_team": str(after.get("team", ""))})
        old_depth = str(before.get("depth_role", ""))
        new_depth = str(after.get("live_depth_role", ""))
        if old_depth != new_depth:
            depth_changes.append({"player_id": pid, "player_name": str(after.get("player_name", "")),
                                  "old_role": old_depth, "new_role": new_depth})
        old_injury = str(before.get("injury_status", "UNKNOWN"))
        new_injury = str(after.get("live_injury_status", "UNKNOWN"))
        if old_injury != new_injury:
            injury_changes.append({"player_id": pid, "player_name": str(after.get("player_name", "")),
                                   "old_status": old_injury, "new_status": new_injury})
    old_tx = pd.DataFrame()
    if len(old_roster) and "gsis_id" in old_roster:
        old_tx = old_roster.dropna(subset=["gsis_id"]).copy()
        old_tx["gsis_id"] = old_tx.gsis_id.astype(str)
        old_tx = old_tx.drop_duplicates("gsis_id", keep="last").set_index("gsis_id")
    tx_changes = []
    if len(old_tx):
        for pid in sorted(old_ids & set(new_all_by.index.astype(str))):
            if pid in old_tx.index:
                before = str(old_tx.loc[pid].get("current_transaction_state", "") or "")
                after_value = new_all_by.loc[pid].get("live_transaction_state", "")
                after = "" if pd.isna(after_value) else str(after_value)
                if before != after:
                    tx_changes.append({"player_id": pid, "player_name": str(new_all_by.loc[pid].get("player_name", "")),
                                       "old_status": before, "new_status": after})
    summary.update({
        "old_official_prediction_player_count": len(old_ids),
        "replacement_prediction_player_count": len(new_ids),
        "players_removed_from_frozen_predictions": sorted(old_ids-new_ids),
        "players_added_to_replacement_state": sorted(new_ids-old_ids),
        "team_changes_vs_frozen_run": team_changes,
        "depth_role_changes_vs_frozen_run": depth_changes,
        "injury_status_changes_vs_frozen_run": injury_changes,
        "transaction_status_changes_vs_frozen_run": tx_changes,
    })
    return summary


def validate_current_player_feature_base(settings, season: int, week: int, cutoff,
                                         baseline_schedule_path) -> dict[str, Any]:
    """Prove global PIT numerical state is current while allowing live roster drift."""
    import json
    from nfl2026.postgame import (
        _expected_games, _frame_digest, _roll_manifest_path, _rollforward_inputs, _target_inputs,
        inspect_completed_week_actuals,
    )

    manifest_path = _roll_manifest_path(settings, season, week)
    feature_path = settings.artifact_root / "phase2" / "player_pregame_universe_features.csv"
    if not manifest_path.exists() or not feature_path.exists():
        raise ValueError("Validated global Week player feature state or manifest is missing")
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_games = _expected_games(settings, season, week - 1)
    actual = inspect_completed_week_actuals(settings, season, week - 1, expected_games)
    if actual.get("status") != "ALREADY_CURRENT":
        raise ValueError(f"Previous-week actuals are not validated/current: {actual.get('status')}")
    schedule, roster = _target_inputs(settings, season, week)
    current = _rollforward_inputs(settings, season, week - 1, week, actual, schedule, roster)
    # The target roster hash is intentionally permitted to differ: this path
    # copies the validated numerical rows and records fresh eligibility in the
    # run-scoped lock. Every historical/source/schema input must still match.
    ignored = {"target_roster_sha256", "target_schedule_sha256"}
    mismatches = [key for key, value in current.items() if key not in ignored and saved.get(key) != value]
    if mismatches:
        raise ValueError(f"Global Week player numerical state is stale or source-mismatched: {mismatches}")
    baseline_schedule = pd.read_csv(baseline_schedule_path, low_memory=False)
    baseline_schedule = baseline_schedule[pd.to_numeric(baseline_schedule.season, errors="coerce").eq(season)
        & pd.to_numeric(baseline_schedule.week, errors="coerce").eq(week)
        & baseline_schedule.game_type.astype(str).str.upper().eq("REG")].copy()
    if _frame_digest(baseline_schedule) != saved.get("target_schedule_sha256"):
        raise ValueError("Frozen reference schedule does not match the schedule used to build player state")
    current_schedule = schedule.copy()
    market_fields = {"away_moneyline", "home_moneyline", "spread_line", "away_spread_odds", "home_spread_odds",
                     "total_line", "over_odds", "under_odds"}
    compare_columns = sorted((set(current_schedule.columns) & set(baseline_schedule.columns)) - market_fields)
    if "game_id" not in compare_columns:
        raise ValueError("Current/reference schedules do not share game identity")
    left = baseline_schedule[compare_columns].sort_values("game_id").reset_index(drop=True).astype(str)
    right = current_schedule[compare_columns].sort_values("game_id").reset_index(drop=True).astype(str)
    if len(left) != len(right) or not left.equals(right):
        raise ValueError("Current non-market Week schedule differs from the feature-state reference")
    if current_schedule[["home_score", "away_score"]].notna().any().any():
        raise ValueError("Current target-week results cannot be used in replacement preparation")
    feature_frame = pd.read_csv(feature_path, low_memory=False)
    target = feature_frame[pd.to_numeric(feature_frame.season, errors="coerce").eq(season)
        & pd.to_numeric(feature_frame.week, errors="coerce").eq(week)]
    cutoff_values = pd.to_datetime(target.prediction_cutoff_utc, utc=True, errors="coerce")
    if target.empty or cutoff_values.isna().any() or cutoff_values.gt(pd.Timestamp(cutoff)).any():
        raise ValueError("Global Week player rows are missing or fail the replacement PIT cutoff")
    return {"status":"VALIDATED_PIT_BASE_WITH_LIVE_ROSTER_OVERLAY","ignored_live_only_manifest_field":"target_roster_sha256",
            "target_rows":len(target),"target_feature_sha256":sha256_file(feature_path),
            "source_completed_week":week-1,"feature_version":saved.get("feature_version"),
            "rollforward_manifest_sha256":sha256_file(manifest_path),"market_schedule_fields_ignored":sorted(market_fields & set(current_schedule.columns))}
