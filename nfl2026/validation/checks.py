from __future__ import annotations

import pandas as pd

from nfl2026.config import Settings


def run_quality_checks(
    settings: Settings,
    games: pd.DataFrame,
    weekly: pd.DataFrame,
    membership: pd.DataFrame,
    team_matrix: pd.DataFrame,
    player_matrix: pd.DataFrame,
    td_recon: pd.DataFrame,
    feature_quality: pd.DataFrame,
    team_predictions: pd.DataFrame | None = None,
    player_predictions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    def add(check_id: str, category: str, severity: str, affected: int, details: str) -> None:
        rows.append({
            "check_id": check_id,
            "category": category,
            "severity": severity,
            "status": "pass" if affected == 0 else ("fail" if severity == "critical" else "warning"),
            "rows_affected": int(affected),
            "details": details,
        })

    add("game_id_unique", "game", "critical", int(games.duplicated("game_id").sum()), "Canonical game_id must be unique.")
    team_fixtures = pd.concat([
        games[["season", "week", "game_type", "home_team", "game_id"]].rename(columns={"home_team": "team"}),
        games[["season", "week", "game_type", "away_team", "game_id"]].rename(columns={"away_team": "team"}),
    ])
    add("team_week_fixture_unique", "game", "critical", int(team_fixtures.duplicated(["season", "week", "game_type", "team"]).sum()), "One fixture per team/week/game type.")
    add("game_teams_present", "game", "critical", int(games[["home_team", "away_team"]].isna().any(axis=1).sum()), "Both teams are required.")
    completed = games[games["result_status"].eq("completed")]
    bad_scores = (~completed["home_score"].between(settings.score_min, settings.score_max)) | (~completed["away_score"].between(settings.score_min, settings.score_max))
    add("score_range", "game", "critical", int(bad_scores.sum()), "Completed scores must be within configured bounds.")
    kickoff = pd.to_datetime(games["kickoff_utc"], errors="coerce", utc=True)
    bad_dates = kickoff.isna() | (kickoff.dt.year < games["season"]) | (kickoff.dt.year > games["season"] + 1)
    add("season_date_consistency", "game", "critical", int(bad_dates.sum()), "Kickoff year must match the NFL season window.")

    key = ["season", "week", "team", "player_id"]
    add("player_week_key_unique", "player", "critical", int(weekly.duplicated(key).sum()), "Stable player-team-week key must be unique.")
    add("stable_player_ids", "player", "critical", int(weekly["player_id"].isna().sum()), "Names are never accepted as primary keys.")
    orphan_memberships = int((~weekly["player_id"].isin(membership["player_id"])).sum())
    add("membership_covers_player_rows", "membership", "critical", orphan_memberships, "Every player-week ID must exist in membership events.")

    team_cutoff = pd.to_datetime(team_matrix["prediction_cutoff_utc"], utc=True)
    player_cutoff = pd.to_datetime(player_matrix["prediction_cutoff_utc"], utc=True)
    add("team_cutoff_present", "leakage", "critical", int(team_cutoff.isna().sum()), "Every team row has a pre-game feature cutoff.")
    add("player_cutoff_present", "leakage", "critical", int(player_cutoff.isna().sum()), "Every player row has a pre-game feature cutoff.")
    target_in_team_features = [c for c in team_matrix if c.startswith("target_") and c.replace("target_", "") in team_matrix.columns]
    add("targets_separated_from_team_features", "leakage", "critical", len(target_in_team_features), "Targets use a target_ namespace and are excluded from model feature lists.")

    bad_td = td_recon[(td_recon["stat"].isin(["rushing_tds", "receiving_tds"])) & td_recon["status"].ne("pass")]
    add("td_label_reconciliation", "labels", "critical", len(bad_td), "PBP-derived TD totals must reconcile with trustworthy 2022-2024 weekly sources.")
    yard_review = td_recon[(td_recon["stat"].str.contains("yards")) & td_recon["status"].ne("pass")]
    add("yardage_reconciliation", "labels", "warning", len(yard_review), "PBP and weekly yardage should reconcile within documented convention tolerance.")

    unusable = feature_quality[feature_quality["flags"].ne("usable")]
    add("feature_quality_flags", "features", "warning", len(unusable), "Flagged features are reported and excluded from fitted baselines.")
    forbidden = [c for c in list(team_matrix.columns) + list(player_matrix.columns) if any(t.lower() in c.lower() for t in settings.forbidden_feature_tokens)]
    add("no_bookmaker_features", "features", "critical", len(forbidden), "Market and bookmaker fields are forbidden from predictive matrices.")

    if team_predictions is not None:
        overlap = pd.to_datetime(team_predictions["max_training_kickoff_utc"], utc=True) >= pd.to_datetime(team_predictions["kickoff_utc"], utc=True)
        add("team_backtest_chronology", "backtest", "critical", int(overlap.sum()), "Training cutoff strictly precedes prediction kickoff.")
    if player_predictions is not None:
        overlap = pd.to_datetime(player_predictions["max_training_kickoff_utc"], utc=True) >= pd.to_datetime(player_predictions["kickoff_utc"], utc=True)
        add("player_backtest_chronology", "backtest", "critical", int(overlap.sum()), "Training cutoff strictly precedes prediction kickoff.")
        invalid_p = (~pd.to_numeric(player_predictions["pred_td_probability"], errors="coerce").between(0, 1)).sum()
        add("probability_bounds", "probability", "critical", int(invalid_p), "All TD probabilities must be in [0, 1].")
    return pd.DataFrame(rows)
