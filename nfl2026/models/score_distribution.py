"""Leakage-safe, event-supported empirical NFL final-score distribution."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import logsumexp


PBP_COLUMNS = [
    "game_id", "play_id", "posteam", "defteam", "td_team", "play_type",
    "special_teams_play", "touchdown", "field_goal_result", "extra_point_result",
    "two_point_conv_result", "extra_point_attempt", "two_point_attempt",
    "defensive_two_point_conv", "safety", "total_home_score", "total_away_score",
]


def _yes(value: object) -> bool:
    return pd.notna(value) and str(value).lower() in {"1", "1.0", "true", "good", "made", "success"}


def extract_team_game_events(pbp: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """Reconcile play-level points to final scores; never infer an ambiguous event."""
    required = set(PBP_COLUMNS)
    if not required.issubset(pbp.columns):
        raise ValueError(f"PBP scoring columns missing: {sorted(required - set(pbp.columns))}")
    games = games[games.game_type.eq("REG")].drop_duplicates("game_id").copy()
    games = games[games.home_score.notna() & games.away_score.notna()]
    source = pbp[pbp.game_id.isin(games.game_id)].sort_values(["game_id", "play_id"]).copy()
    result = []
    for game in games.itertuples(index=False):
        plays = source[source.game_id.eq(game.game_id)]
        if plays.empty:
            continue
        ledger = {game.home_team: defaultdict(int), game.away_team: defaultdict(int)}
        for team in ledger:
            ledger[team]["observed_total_points"] = int(game.home_score if team == game.home_team else game.away_score)
        home_scores = pd.to_numeric(plays.total_home_score, errors="coerce").ffill().fillna(0).to_numpy()
        away_scores = pd.to_numeric(plays.total_away_score, errors="coerce").ffill().fillna(0).to_numpy()
        home_change = np.diff(np.r_[0, home_scores])
        away_change = np.diff(np.r_[0, away_scores])
        for row, dh, da in zip(plays.itertuples(index=False), home_change, away_change):
            if _yes(row.extra_point_attempt) and str(row.posteam) in ledger:
                ledger[str(row.posteam)]["pat_attempted"] += 1
                if not _yes(row.extra_point_result):
                    ledger[str(row.posteam)]["pat_missed"] += 1
            if _yes(row.two_point_attempt) and str(row.posteam) in ledger:
                ledger[str(row.posteam)]["two_point_attempted"] += 1
                if not _yes(row.two_point_conv_result):
                    ledger[str(row.posteam)]["two_point_failed"] += 1
            if dh == 0 and da == 0:
                continue
            if dh < 0 or da < 0 or (dh > 0 and da > 0):
                for counts in ledger.values():
                    counts["ambiguous_plays"] += 1
                continue
            team, points = (game.home_team, int(dh)) if dh > 0 else (game.away_team, int(da))
            counts = ledger[team]
            if _yes(row.touchdown):
                if _yes(row.special_teams_play) or str(row.play_type) in {"kickoff", "punt", "field_goal"}:
                    counts["special_teams_td"] += 1
                elif str(row.td_team) == str(row.posteam) == team:
                    counts["offensive_td"] += 1
                elif str(row.td_team) == str(row.defteam) == team:
                    counts["defensive_td"] += 1
                else:
                    counts["ambiguous_plays"] += 1
                    counts["unclassified_points"] += points
            elif _yes(row.field_goal_result):
                counts["field_goals_made"] += 1
            elif _yes(row.extra_point_result):
                counts["pat_made"] += 1
            elif _yes(row.defensive_two_point_conv):
                counts["defensive_two_point_made"] += 1
            elif _yes(row.two_point_conv_result):
                counts["two_point_made"] += 1
            elif _yes(row.safety):
                counts["safeties"] += 1
            else:
                counts["ambiguous_plays"] += 1
                counts["unclassified_points"] += points
        for team, counts in ledger.items():
            reconstructed = (
                6 * (counts["offensive_td"] + counts["defensive_td"] + counts["special_teams_td"])
                + 3 * counts["field_goals_made"] + counts["pat_made"]
                + 2 * (counts["two_point_made"] + counts["defensive_two_point_made"] + counts["safeties"])
            )
            is_home = team == game.home_team
            pbp_final = int(home_scores[-1] if is_home else away_scores[-1])
            result.append({
                "game_id": game.game_id, "season": int(game.season), "week": int(game.week),
                "team": team, "opponent": game.away_team if is_home else game.home_team,
                "side": "HOME" if is_home else "AWAY",
                "kickoff_utc": game.kickoff_utc, **dict(counts),
                "reconstructed_points": reconstructed, "pbp_final_points": pbp_final,
                "event_reconciliation_valid": (
                    reconstructed == counts["observed_total_points"] == pbp_final
                ),
            })
    return pd.DataFrame(result).fillna(0)


def feasible_totals(events: pd.DataFrame, maximum: int = 70) -> np.ndarray:
    """Derive arithmetic support from observed component count maxima."""
    td = events[["offensive_td", "defensive_td", "special_teams_td"]].sum(axis=1)
    max_td = int(td.max())
    max_fg = int(events.field_goals_made.max())
    max_safety = int(events.safeties.max())
    max_def_two = int(events.defensive_two_point_made.max())
    possible = {0}
    for t in range(max_td + 1):
        for fg in range(max_fg + 1):
            for pat in range(t + 1):
                for two in range(t - pat + 1):
                    for safety in range(max_safety + 1):
                        for def_two in range(max_def_two + 1):
                            score = 6*t + 3*fg + pat + 2*(two + safety + def_two)
                            if score <= maximum:
                                possible.add(score)
    return np.array(sorted(possible), dtype=int)


def fit_joint_empirical(events: pd.DataFrame, dependence: float = 0.35, alpha: float = 0.25) -> dict:
    valid = events[events.event_reconciliation_valid.astype(bool)].copy()
    pairs = valid.pivot(index="game_id", columns="side", values="observed_total_points").dropna()
    if len(pairs) < 100:
        raise ValueError("At least 100 reconciled historical games are required")
    if not 0 <= dependence <= 1:
        raise ValueError("Dependence mixture must be within [0, 1]")
    support = feasible_totals(valid)
    max_score = int(max(support.max(), pairs.to_numpy().max()))
    h = pairs.HOME.astype(int).to_numpy()
    a = pairs.AWAY.astype(int).to_numpy()
    hcount = np.bincount(h, minlength=max_score+1).astype(float)
    acount = np.bincount(a, minlength=max_score+1).astype(float)
    feasible = np.zeros(max_score+1, dtype=bool)
    feasible[support] = True
    hp = (hcount + alpha * feasible) / (hcount.sum() + alpha * feasible.sum())
    ap = (acount + alpha * feasible) / (acount.sum() + alpha * feasible.sum())
    joint_count = np.zeros((max_score+1, max_score+1), dtype=float)
    np.add.at(joint_count, (h, a), 1)
    joint = joint_count / joint_count.sum()
    base = (1-dependence)*np.outer(hp, ap) + dependence*joint
    return {
        "base": base, "support": support, "training_games": len(pairs),
        "training_seasons": sorted(valid.season.astype(int).unique().tolist()),
        "dependence": dependence, "alpha": alpha,
    }


def joint_distribution(model: dict, expected_home: float, expected_away: float, bandwidth: float | None = None) -> np.ndarray:
    """Minimum-KL tilt of a joint historical event prior to the precise model means."""
    base = np.asarray(model["base"], dtype=float)
    if not np.isfinite([expected_home, expected_away]).all():
        raise ValueError("Expected points must be finite")
    points = np.arange(base.shape[0], dtype=float)
    xh, xa = np.meshgrid(points, points, indexing="ij")
    logbase = np.full(base.shape, -np.inf)
    positive = base > 0
    logbase[positive] = np.log(base[positive])
    if bandwidth is not None:
        if bandwidth <= 0:
            raise ValueError("Bandwidth must be positive")
        logbase -= ((xh-expected_home)**2 + (xa-expected_away)**2 - 0.24*(xh-expected_home)*(xa-expected_away)) / (2*bandwidth**2*(1-0.12**2))
    theta = np.zeros(2)
    target = np.array([expected_home, expected_away], dtype=float)
    for _ in range(40):
        z = logbase + theta[0]*xh + theta[1]*xa
        p = np.exp(z - logsumexp(z))
        mean = np.array([np.sum(p*xh), np.sum(p*xa)])
        error = target - mean
        if np.max(np.abs(error)) < 1e-7:
            return p
        hh = np.sum(p*(xh-mean[0])**2)
        aa = np.sum(p*(xa-mean[1])**2)
        ha = np.sum(p*(xh-mean[0])*(xa-mean[1]))
        step = np.linalg.solve(np.array([[hh, ha], [ha, aa]]) + np.eye(2)*1e-8, error)
        theta += np.clip(step, -0.5, 0.5)
    raise ValueError("Joint score distribution could not preserve expected points")


def score_summary(p: np.ndarray, top_n: int = 5) -> dict:
    if abs(float(p.sum())-1) > 1e-8 or (p < 0).any():
        raise ValueError("Invalid joint probability mass")
    order = np.argsort(p.ravel())[::-1][:top_n]
    top = [
        {"home_score": int(i//p.shape[1]), "away_score": int(i%p.shape[1]),
         "probability": float(p.ravel()[i])}
        for i in order
    ]
    hp, ap = p.sum(axis=1), p.sum(axis=0)
    scores = np.arange(p.shape[0])
    margin = scores[:, None]-scores[None, :]
    total = scores[:, None]+scores[None, :]
    margin_p = np.bincount((margin+len(scores)-1).ravel(), weights=p.ravel(), minlength=2*len(scores)-1)
    total_p = np.bincount(total.ravel(), weights=p.ravel(), minlength=2*len(scores)-1)
    def quantiles(prob, offset=0):
        return {f"p{q}": int(np.searchsorted(np.cumsum(prob), q/100)+offset) for q in (10,25,50,75,90)}
    return {
        "headline_home_score": top[0]["home_score"],
        "headline_away_score": top[0]["away_score"],
        "headline_exact_score_probability": top[0]["probability"],
        "top_scorelines": top,
        "home_quantiles": quantiles(hp), "away_quantiles": quantiles(ap),
        "margin_quantiles": quantiles(margin_p, 1-len(scores)), "total_quantiles": quantiles(total_p),
        "discrete_home_mean": float(np.dot(scores, hp)),
        "discrete_away_mean": float(np.dot(scores, ap)),
        "discrete_margin_mean": float(np.sum(p*margin)),
        "discrete_total_mean": float(np.sum(p*total)),
        "home_away_score_correlation": float(
            np.sum(p*(scores[:,None]-np.dot(scores,hp))*(scores[None,:]-np.dot(scores,ap)))
            / np.sqrt(np.dot((scores-np.dot(scores,hp))**2,hp)*np.dot((scores-np.dot(scores,ap))**2,ap))
        ),
    }


def read_historical_scoring(root: Path, seasons: tuple[int, ...] = (2022,2023,2024,2025)) -> pd.DataFrame:
    games = pd.read_csv(root/"artifacts"/"canonical"/"canonical_games.csv", low_memory=False)
    parts = []
    for season in seasons:
        source = root/"NFL Fantasy"/f"{season} Stats"/f"pbp_{season}_{season}_sample50k.csv"
        frame = pd.read_csv(source, usecols=PBP_COLUMNS, low_memory=False)
        part = extract_team_game_events(frame, games[games.season.eq(season)])
        parts.append(part)
    return pd.concat(parts, ignore_index=True)
