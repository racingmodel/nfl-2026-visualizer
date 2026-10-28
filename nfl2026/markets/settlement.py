from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from nfl2026.config import Settings
from nfl2026.markets.ledger import ensure_ledger
from nfl2026.operations import official_value_freeze


def settle_value(selection: pd.Series | dict, actual: dict) -> tuple[str, float]:
    row = dict(selection)
    market = str(row["market_type"])
    if market not in {'MONEYLINE','SPREAD','TOTAL','ANYTIME_TD','RUSHING_YARDS','RECEIVING_YARDS'}:
        raise ValueError('Unsupported legacy settlement; use a matching certified settlement contract')
    odds = float(row["book_odds"])
    result = "LOSS"
    if market == "MONEYLINE":
        if actual.get('winner_team') not in {actual.get('home_team'),actual.get('away_team')}:
            raise ValueError('Missing winner/tie settlement contract')
        result = "WIN" if str(row["team"]) == str(actual["winner_team"]) else "LOSS"
    elif market == "SPREAD":
        team_score = float(actual["home_score"] if row["team"] == actual["home_team"] else actual["away_score"])
        opponent_score = float(actual["away_score"] if row["team"] == actual["home_team"] else actual["home_score"])
        adjusted = team_score - opponent_score + float(row["line"])
        if not math.isfinite(adjusted): raise ValueError('Missing final scores/line')
        result = "WIN" if adjusted > 0 else "PUSH" if adjusted == 0 else "LOSS"
    elif market == "TOTAL":
        difference = float(actual["home_score"]) + float(actual["away_score"]) - float(row["line"])
        if not math.isfinite(difference): raise ValueError('Missing final scores/line')
        result = "WIN" if (row["side"] == "OVER" and difference > 0) or (row["side"] == "UNDER" and difference < 0) else "PUSH" if difference == 0 else "LOSS"
    elif market == "ANYTIME_TD":
        if str(row['player_id']) not in actual.get('td_eligible_ids',[]): raise ValueError('Missing player TD/participation evidence')
        scored = str(row["player_id"]) in {str(value) for value in actual['td_scorer_ids']}
        result = 'WIN' if (scored if row.get('side','YES') == 'YES' else not scored) else 'LOSS'
    elif market in {"RUSHING_YARDS", "RECEIVING_YARDS"}:
        value_key = "rushing_yards" if market == "RUSHING_YARDS" else "receiving_yards"
        difference = float(actual[value_key][str(row["player_id"])]) - float(row["line"])
        if not math.isfinite(difference): raise ValueError('Missing player actual; never zero-fill for settlement')
        result = "WIN" if (row["side"] == "OVER" and difference > 0) or (row["side"] == "UNDER" and difference < 0) else "PUSH" if difference == 0 else "LOSS"
    gross_return = odds if result == "WIN" else 1.0 if result == "PUSH" else 0.0
    return result, gross_return


def performance_summary(ledger: pd.DataFrame) -> pd.DataFrame:
    settled = ledger[ledger.settlement_status.eq("SETTLED")].copy()
    if settled.empty:
        return pd.DataFrame(columns=["market_type", "selections", "wins", "losses", "pushes", "stake", "return", "roi"])
    rows = []
    for market, group in settled.groupby("market_type"):
        stake = pd.to_numeric(group.hypothetical_stake).sum()
        returned = pd.to_numeric(group.hypothetical_return).sum()
        rows.append({"market_type": market, "selections": len(group), "wins": int(group.settlement_result.eq("WIN").sum()), "losses": int(group.settlement_result.eq("LOSS").sum()), "pushes": int(group.settlement_result.eq("PUSH").sum()), "stake": stake, "return": returned, "roi": (returned - stake) / stake if stake else 0.0})
    return pd.DataFrame(rows)


def performance_breakdown(ledger: pd.DataFrame) -> pd.DataFrame:
    settled = ledger[ledger.settlement_status.eq("SETTLED")].copy()
    columns = ["dimension", "segment", "selections", "wins", "losses", "pushes", "stake", "return", "profit", "roi"]
    if settled.empty:
        return pd.DataFrame(columns=columns)
    settled["edge_band"] = pd.cut(pd.to_numeric(settled.edge_pp), [-float("inf"), 5, 10, float("inf")], labels=["UNDER_5PP", "5_TO_10PP", "OVER_10PP"])
    rows = []
    for dimension in ("market_type", "value_class", "value_confidence", "edge_band"):
        for segment, group in settled.groupby(dimension, observed=True):
            stake = float(pd.to_numeric(group.hypothetical_stake).sum())
            returned = float(pd.to_numeric(group.hypothetical_return).sum())
            rows.append({"dimension": dimension, "segment": str(segment), "selections": len(group), "wins": int(group.settlement_result.eq("WIN").sum()), "losses": int(group.settlement_result.eq("LOSS").sum()), "pushes": int(group.settlement_result.eq("PUSH").sum()), "stake": stake, "return": returned, "profit": returned - stake, "roi": (returned - stake) / stake if stake else 0.0})
    return pd.DataFrame(rows, columns=columns)


def settle_official_ledger(settings: Settings, season: int, week: int, actual_dir: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    path = ensure_ledger(settings)
    ledger = pd.read_csv(path)
    if ledger.empty:
        return ledger, performance_breakdown(ledger)
    if actual_dir is None:
        root = settings.artifact_root / "actuals" / str(season) / f"week_{week:02d}"
        choices = sorted((candidate for candidate in root.glob("actual-*") if candidate.is_dir()), key=lambda candidate: candidate.stat().st_mtime)
        if not choices:
            raise FileNotFoundError(f"No actuals snapshot for {season} Week {week}")
        actual_dir = choices[-1]
    games = pd.read_csv(actual_dir / "team_actuals.csv")
    players = pd.read_csv(actual_dir / "player_actuals.csv")
    freeze = official_value_freeze(settings, season, week)
    official_by_game = freeze.set_index("game_id").official_value_run_id.astype(str).to_dict() if len(freeze) else {}
    completed = set(games.game_id.astype(str))
    for index, row in ledger[ledger.settlement_status.eq("OPEN")].iterrows():
        game_id = str(row.game_id)
        if game_id not in completed:
            continue
        if official_by_game.get(game_id) != str(row.value_run_id):
            ledger.loc[index, "settlement_status"] = "SUPERSEDED_PREKICKOFF"
            continue
        game = games[games.game_id.astype(str).eq(game_id)].iloc[0]
        game_players = players[players.game_id.astype(str).eq(game_id)] if "game_id" in players else players[players.team.astype(str).isin([str(game.home_team), str(game.away_team)])]
        actual = {
            "home_team": game.home_team,
            "away_team": game.away_team,
            "home_score": game.home_score,
            "away_score": game.away_score,
            "winner_team": game.winner,
            "td_scorer_ids": game_players[pd.to_numeric(game_players.actual_td, errors="coerce").gt(0)].player_id.astype(str).tolist(),
            "td_eligible_ids": game_players[pd.to_numeric(game_players.actual_td, errors='coerce').notna()].player_id.astype(str).tolist(),
            "rushing_yards": dict(zip(game_players.player_id.astype(str), pd.to_numeric(game_players.rushing_yards, errors="coerce"))),
            "receiving_yards": dict(zip(game_players.player_id.astype(str), pd.to_numeric(game_players.receiving_yards, errors="coerce"))),
        }
        try:
            result, returned = settle_value(row, actual)
        except (KeyError, TypeError, ValueError):
            ledger.loc[index, "settlement_status"] = "SETTLEMENT_BLOCKED"
            continue
        ledger.loc[index, "settlement_status"] = "SETTLED"
        ledger.loc[index, "settlement_result"] = result
        ledger.loc[index, "hypothetical_return"] = returned
    ledger.to_csv(path, index=False, lineterminator="\n")
    report = performance_breakdown(ledger)
    report_path = settings.report_root / "live_2026_value_performance.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(report_path, index=False, lineterminator="\n")
    return ledger, report
