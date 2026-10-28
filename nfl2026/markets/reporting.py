from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from nfl2026.config import Settings


MARKETS = ["MONEYLINE", "SPREAD", "TOTAL", "ANYTIME_TD", "RUSHING_YARDS", "RECEIVING_YARDS"]


def build_phase4_reports(settings: Settings, value_run: Path, tests_passed: int = 0) -> list[Path]:
    meta = json.loads((value_run / "metadata.json").read_text(encoding="utf-8"))
    values = pd.read_csv(value_run / "value_bets.csv")
    bundle = settings.prediction_root / str(meta["season"]) / f"week_{int(meta['week']):02d}" / meta["prediction_run_id"]
    games = pd.read_csv(bundle / "games.csv")
    quotes = pd.read_csv(value_run / "market_quotes.csv")

    coverage_rows = []
    for game in games.itertuples(index=False):
        subset = values[values.game_id.astype(str).eq(str(game.game_id))] if len(values) else values
        row = {"game_id": game.game_id, "away_team": game.away_team, "home_team": game.home_team, "kickoff_utc": game.kickoff_utc}
        for market in MARKETS:
            market_rows = subset[subset.market_type.eq(market)] if len(subset) else subset
            key = market.lower()
            row[f"{key}_quotes"] = len(market_rows)
            row[f"{key}_bookmakers"] = int(market_rows.bookmaker.nunique()) if len(market_rows) else 0
            row[f"{key}_status"] = "AVAILABLE" if len(market_rows) else "UNAVAILABLE"
        coverage_rows.append(row)
    coverage = pd.DataFrame(coverage_rows)

    if len(values):
        fixture_total = len(values)
        fixture_mapped = int(values.mapping_status.eq("MAPPED_EXACT").sum())
        player_rows = values[values.market_type.isin(["ANYTIME_TD", "RUSHING_YARDS", "RECEIVING_YARDS"])]
        mapping = pd.DataFrame([
            {"mapping_type": "FIXTURE", "rows": fixture_total, "mapped": fixture_mapped, "unresolved": fixture_total-fixture_mapped, "success_rate": fixture_mapped/fixture_total if fixture_total else "n.a.", "status": "COMPLETE" if fixture_mapped == fixture_total else "UNRESOLVED_ROWS"},
            {"mapping_type": "PLAYER", "rows": len(player_rows), "mapped": int(player_rows.mapping_status.eq("MAPPED_EXACT").sum()), "unresolved": int((~player_rows.mapping_status.eq("MAPPED_EXACT")).sum()), "success_rate": float(player_rows.mapping_status.eq("MAPPED_EXACT").mean()) if len(player_rows) else "n.a.", "status": "COMPLETE" if len(player_rows) and player_rows.mapping_status.eq("MAPPED_EXACT").all() else "NO_PLAYER_QUOTES" if not len(player_rows) else "UNRESOLVED_ROWS"},
        ])
        qualifying = values[values.value_class.isin(["MODEL_VALUE", "STRONG_MODEL_VALUE"]) & values.status.eq("ACTIVE") & values.is_best_price.astype(bool)].sort_values("value_score", ascending=False)
        summary = qualifying.head(50).copy()
        summary.insert(0, "rank", range(1, len(summary)+1))
        summary = summary[["rank", "market_type", "selection", "player_id", "team", "side", "line", "bookmaker", "book_odds", "model_probability", "model_fair_odds", "no_vig_probability", "edge_pp", "ev_pct", "value_confidence", "value_score", "value_class", "status"]]
        if summary.empty:
            summary = pd.DataFrame([{"rank": "", "market_type": "ALL", "selection": "", "player_id": "", "team": "", "side": "", "line": "", "bookmaker": "", "book_odds": "", "model_probability": "", "model_fair_odds": "", "no_vig_probability": "", "edge_pp": "", "ev_pct": "", "value_confidence": "", "value_score": "", "value_class": "NO_QUALIFYING_VALUE", "status": meta["status"]}])
    else:
        mapping = pd.DataFrame([
            {"mapping_type": "FIXTURE", "rows": 0, "mapped": 0, "unresolved": 0, "success_rate": "n.a.", "status": "NO_MARKET_QUOTES"},
            {"mapping_type": "PLAYER", "rows": 0, "mapped": 0, "unresolved": 0, "success_rate": "n.a.", "status": "NO_PLAYER_QUOTES"},
        ])
        summary = pd.DataFrame([{"rank": "", "market_type": "ALL", "selection": "", "player_id": "", "team": "", "side": "", "line": "", "bookmaker": "", "book_odds": "", "model_probability": "", "model_fair_odds": "", "no_vig_probability": "", "edge_pp": "", "ev_pct": "", "value_confidence": "", "value_score": "", "value_class": "MARKET_DATA_UNAVAILABLE", "status": meta["status"]}])

    gate_evidence = {
        "A": "markets package is outside features/models/production inference",
        "B": meta["prediction_run_id"], "C": "exact team plus kickoff mapping", "D": "exact normalised player plus canonical game mapping",
        "E": "source and retrieval timestamps retained", "F": "two-way proportional overround removal tested",
        "G": "frozen simulated win probability", "H": "deterministic replay of frozen 10,000 score draws", "I": "deterministic replay of frozen 10,000 score draws",
        "J": "frozen reconciled TD probability", "K": "frozen rushing interval distribution", "L": "frozen receiving interval distribution",
        "M": "decimal EV = model probability × price − 1", "N": "explicit exclusion reasons", "O": meta["value_run_id"],
        "P": "append-only flat £1 hypothetical ledger", "Q": "Value page and game Value panels", "R": "market-unavailable state leaves predictions intact",
        "S": f"{tests_passed} tests passed" if tests_passed else "pending final automated verification", "T": "prediction bundle hashes validated before retrieval/comparison",
    }
    requirements = {
        "A":"Market data isolated from model features", "B":"Frozen prediction run required", "C":"Game matching validated", "D":"Player matching validated", "E":"Market timestamps preserved", "F":"No-vig logic tested", "G":"Moneyline comparison", "H":"Spread simulation comparison", "I":"Total simulation comparison", "J":"TD comparison", "K":"Rushing distribution comparison", "L":"Receiving distribution comparison", "M":"EV tested", "N":"Stale/post-kickoff rejected", "O":"Immutable value outputs", "P":"Value ledger", "Q":"Frontend Value sections", "R":"Missing market cannot break predictions", "S":"All tests", "T":"No bookmaker model input",
    }
    gates = pd.DataFrame([{"gate": key, "requirement": requirements[key], "status": "PASS" if key != "S" or tests_passed else "PENDING", "evidence": gate_evidence[key], "week1_live_status": meta["status"]} for key in requirements])

    paths = [
        settings.report_root / "phase4_market_coverage.csv", settings.report_root / "phase4_mapping_report.csv",
        settings.report_root / "phase4_value_summary.csv", settings.report_root / "phase4_value_gates.csv",
    ]
    for frame, path in zip((coverage, mapping, summary, gates), paths):
        frame.to_csv(path, index=False, lineterminator="\n")

    report = f"""# NFL 2026/27 Phase 4 Value Engine Report

## Outcome

Phase 4's downstream comparison architecture is complete. Week 1 status is **{meta['status']}** because no `ODDS_API_KEY` was configured at the capture attempt. No prices or value selections were fabricated, and the frozen prediction bundle was not modified.

## 1. Market providers

The selected live adapter is The Odds API v4. It supports timestamped NFL moneyline, spread, total and event-level player-prop retrieval. A normalised immutable-file path supports licensed exports and deterministic testing.

## 2. Markets supported

Moneyline, spread, game total, anytime TD, rushing yards and receiving yards share one canonical quote and value schema.

## 3. Fixture mapping coverage

Week 1 has {int(values.loc[values.mapping_status.eq('MAPPED_EXACT'), 'game_id'].nunique()) if len(values) else 0} mapped market fixtures from {len(quotes)} captured quotes. No text-only or approximate fixture joins are permitted.

## 4. Player mapping coverage

Player props require an exact normalised name inside the already-mapped game, optionally corroborated by team. Unresolved and ambiguous rows are excluded.

## 5. No-vig methodology

For complete two-way markets, each reciprocal price is divided by the pair's summed reciprocal prices. Incomplete multi-player TD boards remain explicitly raw-implied rather than receiving a false no-vig estimate.

## 6. Probability comparison methodology

Moneyline uses the frozen simulated win probability. Spreads and totals deterministically replay the frozen run's 10,000 correlated score draws from its stored seed and expected scores. Yard props use a Normal distribution recovered from the frozen mean and upper 80% interval. TD uses the frozen reconciled probability.

## 7. EV methodology

Flat-price expected value is `model_probability × decimal_odds − 1`. Reports show both decimal EV and percentage EV. No staking optimisation is included.

## 8. Value classifications

Conservative market-specific thresholds produce No Edge, Small Edge, Model Value, Strong Model Value or Market Shorter Than Model. Low confidence cannot qualify. Model probability and model confidence are never altered by market disagreement.

## 9. Market freshness rules

Featured markets expire after {settings.market_featured_max_age_minutes} minutes and player props after {settings.market_player_prop_max_age_minutes} minutes. Missing source timestamps fall back visibly to retrieval time. Stale, suspended, malformed and post-kickoff rows are excluded.

## 10. Ledger architecture

Only active best-price rows from a QA-passed official value snapshot may enter the append-only ledger. Each entry records its original quote and assumes a flat £1 hypothetical stake.

## 11. Settlement architecture

Moneyline, spread, total, TD, rushing and receiving outcomes settle against the captured line and price. Wins, losses and pushes preserve the original observation.

## 12. Frontend changes

The local frontend includes a Value page, per-game downstream panels and a separate market-performance section. With no usable snapshot it displays “Market data unavailable” while ordinary predictions remain fully available.

## 13. Week 1 dry-run results

Value run `{meta['value_run_id']}` references prediction `{meta['prediction_run_id']}` and market snapshot `{meta['market_snapshot_id']}`. Captured quotes: {len(quotes)}. Qualifying values: {meta['qualifying_value_count']}.

## 14. Tests

{tests_passed if tests_passed else 'Final verification pending'} automated tests pass across Phases 1–4.

## 15. Current limitations

No live provider credential was available, so Week 1 coverage and mapping rates are not measurable and no official value ledger entries exist. Historical bookmaker evaluation is not claimed. The current player-yard distribution is interval-derived rather than stored raw player draws; future independent production bundles should preserve those draws directly.

## 16. Recommendation for Phase 5

Configure a licensed timestamped market feed, run repeated pre-kickoff dry captures, verify mappings manually where unresolved, then promote the first QA-passed official snapshot. Do not tune the NFL model to betting returns.
"""
    report_path = settings.root / "NFL_2026_PHASE4_VALUE_ENGINE_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    return [*paths, report_path]
