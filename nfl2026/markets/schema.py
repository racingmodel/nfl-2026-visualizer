from __future__ import annotations

MARKET_QUOTE_COLUMNS = [
    "quote_id", "provider", "provider_event_id", "bookmaker", "market_type",
    "selection", "selection_team", "side", "line", "decimal_odds",
    "home_team_name", "away_team_name", "kickoff_utc", "player_name",
    "player_team", "player_position", "player_id", "source_timestamp_utc",
    "retrieval_timestamp_utc", "suspended",
    "period", "overtime", "stat_contract", "jurisdiction", "rule_version",
    "grading_rule_evidence", "quote_type", "venue", "executable", "available_size",
    "requested_backer_stake", "offer_verified", "commission_rate",
]

VALUE_COLUMNS = [
    "value_run_id", "prediction_run_id", "prediction_lock_id", "prediction_timestamp_utc",
    "prediction_anchor_run_id", "prediction_reused_immutable",
    "market_snapshot_id", "quote_id", "game_id", "market_type", "selection",
    "player_id", "player_name", "team", "side", "line", "bookmaker", "book_odds",
    "market_timestamp_utc", "market_age_minutes", "model_probability", "model_fair_odds",
    "book_implied_probability", "no_vig_probability", "no_vig_status",
    "consensus_probability", "book_count", "edge_pp", "ev_decimal", "ev_pct",
    "model_mean", "model_difference", "distribution_method", "model_confidence",
    "role_confidence", "role_label", "confidence_notes", "value_confidence", "value_score", "value_class",
    "opening_book_odds", "price_move", "movement_direction", "is_best_price",
    "comparison_basis", "mapping_status", "exclusion_reason", "status",
]
from nfl2026.markets.certified_value import EXTRA_COLUMNS
VALUE_COLUMNS += EXTRA_COLUMNS

LEDGER_COLUMNS = [
    "ledger_entry_id", "captured_at_utc", "value_run_id", "prediction_run_id",
    "prediction_lock_id", "market_snapshot_id", "quote_id", "game_id", "market_type",
    "selection", "player_id", "team", "side", "line", "bookmaker", "book_odds", "market_timestamp_utc",
    "model_probability", "model_fair_odds", "no_vig_probability", "edge_pp", "ev_pct",
    "value_confidence", "value_class", "hypothetical_stake", "settlement_status",
    "settlement_result", "hypothetical_return",
]
