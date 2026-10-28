"""Challenger opportunity-allocation simulation, NOT a production scoring engine.

Shared paired OOS team-volume residuals couple opponents; multinomial carries and
targets couple teammates. Receptions cannot exceed targets. Efficiency samples
come from historical observed yards/opportunity, including negative values.
Team passing yardage is the sum of receiving yardage (gross, not net of sacks).
Untargeted passes, sacks, team kneels, defensive/ST scores remain outside this
prototype and are explicitly reported. No final score or certified odds emitted.
"""
from __future__ import annotations

import time
import numpy as np
import pandas as pd

from nfl2026.five_week_review import finite
from nfl2026.phase7.common import json_write


def crps_samples(samples, actual):
    samples = np.asarray(samples, float)
    ordered = np.sort(samples, axis=0)
    n = len(ordered)
    weights = (2 * np.arange(1, n+1) - n - 1).reshape((-1,) + (1,) * (samples.ndim-1))
    return np.mean(np.abs(samples - np.asarray(actual)), axis=0) - np.sum(weights * ordered, axis=0) / n**2


def weights(values):
    values = np.asarray(values, float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Allocation weights must be finite/nonnegative")
    return values / values.sum() if values.sum() else np.zeros_like(values)


def draw_joint_usage(players: pd.DataFrame, team_order: list[str], residual_pairs: np.ndarray,
                     efficiency_pools: dict, *, draws: int, seed: int) -> dict:
    """Generate research draws from existing pregame opportunity/catch forecasts.

    residual_pairs shape N x 4 is home rush,target,away rush,target. Required
    metadata/chronology is validated by research_validation before fitting it.
    No scores, game status, bookmaker fields or actual target outcomes are read.
    """
    required = {"team", "position", "pred_carries", "pred_targets", "catch_probability"}
    if not required.issubset(players) or len(team_order) != 2 or len(set(team_order)) != 2 or set(players.team) != set(team_order):
        raise ValueError("Simulation player/team contract invalid")
    if not set(players.position).issubset({"QB", "RB", "FB", "WR", "TE"}):
        raise ValueError("Defensive/nonoffensive players cannot enter the usage simulator")
    if not players.catch_probability.between(0, 1).all():
        raise ValueError("Catch probability out of bounds")
    residual_pairs = np.asarray(residual_pairs, float)
    if residual_pairs.ndim != 2 or residual_pairs.shape[1] != 4 or not len(residual_pairs) or not np.isfinite(residual_pairs).all():
        raise ValueError("Paired OOS volume residuals required")
    if draws < 1:
        raise ValueError("Positive draw count required")
    rng = np.random.default_rng(seed)
    residual = residual_pairs[rng.integers(0, len(residual_pairs), draws)]
    # Centre residuals only using the already fitted calibration fold. No outcome
    # from the game being simulated contributes to this location correction.
    residual = residual - residual_pairs.mean(axis=0)
    n = len(players)
    carries, targets, receptions = (np.zeros((draws, n), dtype=np.int32) for _ in range(3))
    rush, receive = (np.zeros((draws, n), dtype=float) for _ in range(2))
    team_counts = np.zeros((draws, 4), dtype=np.int32)
    for side, team in enumerate(team_order):
        ids = np.flatnonzero(players.team.eq(team).to_numpy())
        for target, matrix, offset in (("carries", carries, 0), ("targets", targets, 1)):
            w = weights(players.iloc[ids][f"pred_{target}"])
            total = float(players.iloc[ids][f"pred_{target}"].sum())
            counts = np.maximum(0, np.rint(total + residual[:, 2*side+offset])).astype(int)
            if w.sum() == 0:
                counts[:] = 0
            team_counts[:, 2*side+offset] = counts
            for count in np.unique(counts):
                mask = counts == count
                matrix[np.ix_(mask, ids)] = rng.multinomial(int(count), w, int(mask.sum())) if w.sum() else 0
    receptions[:] = rng.binomial(targets, players.catch_probability.to_numpy())
    for i, position in enumerate(players.position):
        for kind, counts, matrix in (("rush", carries, rush), ("receive", receptions, receive)):
            pool = np.asarray(efficiency_pools.get((kind, position), efficiency_pools.get((kind, "ALL"), [])), float)
            if not len(pool) or not np.isfinite(pool).all():
                raise ValueError(f"Historical efficiency coverage missing: {kind}/{position}")
            # A game-level efficiency draw preserves role dispersion; negative
            # historical efficiency remains possible. This is research, not a
            # claim of calibrated per-play independence.
            edges = efficiency_pools.get((kind, "edges"))
            if edges is None:
                matrix[:, i] = counts[:, i] * rng.choice(pool, size=draws)
            else:
                buckets = np.searchsorted(edges, counts[:, i])
                for bucket in np.unique(buckets):
                    mask = buckets == bucket
                    conditional = efficiency_pools.get((kind, position, int(bucket)),
                        efficiency_pools.get((kind, "ALL", int(bucket)), pool))
                    matrix[mask, i] = counts[mask, i] * rng.choice(conditional, size=int(mask.sum()))
    team_receiving = np.column_stack([receive[:, players.team.eq(t)].sum(axis=1) for t in team_order])
    return {"carries": carries, "targets": targets, "receptions": receptions,
            "rushing_yards": rush, "receiving_yards": receive,
            "team_volume": team_counts, "team_gross_passing_yards": team_receiving,
            "state": "EXPERIMENTAL_UNCALIBRATED", "excluded": ["untargeted passes", "sacks", "kneels", "TD allocation", "final scores"]}


def conditional_efficiency_pools(weekly, minimum=50):
    """Prior calibration data only; opportunity buckets reduce tiny-sample tails."""
    pools = {}
    for kind, count, yards in (("rush", "carries", "rushing_yards"), ("receive", "receptions", "receiving_yards")):
        valid = weekly[weekly[count].gt(0) & weekly[yards].notna()].copy()
        if valid.empty:
            raise ValueError("No historical efficiency observations")
        valid["rate"] = valid[yards] / valid[count]
        edges = np.unique(np.quantile(valid[count], [1/3, 2/3]))
        pools[(kind, "edges")] = edges
        valid["bucket"] = np.searchsorted(edges, valid[count])
        pools[(kind, "ALL")] = valid.rate.to_numpy()
        for bucket, part in valid.groupby("bucket"):
            if len(part) >= minimum:
                pools[(kind, "ALL", int(bucket))] = part.rate.to_numpy()
        for position, part in valid.groupby("position"):
            if len(part) >= minimum:
                pools[(kind, position)] = part.rate.to_numpy()
            for bucket, group in part.groupby("bucket"):
                if len(group) >= minimum:
                    pools[(kind, position, int(bucket))] = group.rate.to_numpy()
    return pools


def evaluate_joint_refinement(settings):
    """Same 272 games, meaningful positional cohorts, no source/model refitting."""
    from nfl2026.phase7.player_v4 import population_mask, KEYS
    frame = pd.read_csv(settings.artifact_root / "research/phase7/player_v4_oos.csv")
    frame = frame[frame.model.eq("phase7-player-v4-opportunity-research-v1-extra_trees")]
    train, test = frame[frame.season.le(2024)], frame[frame.season.eq(2025)]
    if (pd.to_datetime(train.max_training_kickoff_utc, utc=True) >= pd.to_datetime(train.kickoff_utc, utc=True)).any():
        raise ValueError("Joint residuals must be OOS")
    if pd.to_datetime(train.kickoff_utc, utc=True).max() >= pd.to_datetime(test.kickoff_utc, utc=True).min():
        raise ValueError("Joint calibration entered target fold")
    reference = pd.read_csv(settings.artifact_root / "research/five_week_review/player_production_semantics_replay.csv")
    test = test.merge(reference[KEYS+["pred_rushing_yards", "pred_receiving_yards"]], on=KEYS, validate="one_to_one", suffixes=("", "_champion"))
    games = pd.read_csv(settings.artifact_root / "canonical/canonical_games.csv")
    volume = train.groupby(["game_id", "team"])[["pred_carries", "pred_targets", "actual_carries", "actual_targets"]].sum()
    pairs = []
    for game in games[games.game_id.isin(train.game_id)].itertuples():
        values = []
        for team in (game.home_team, game.away_team):
            row = volume.loc[(game.game_id, team)]
            values.extend([row.actual_carries-row.pred_carries, row.actual_targets-row.pred_targets])
        pairs.append(values)
    residuals = np.asarray(pairs)
    weekly = pd.read_csv(settings.artifact_root / "canonical/player_weekly.csv", low_memory=False)
    pools = conditional_efficiency_pools(weekly[weekly.season.le(2024) & weekly.player_id.notna()])
    rows = []
    started = time.perf_counter()
    for game in games[games.game_id.isin(test.game_id)].itertuples():
        players = test[test.game_id.eq(game.game_id)].reset_index(drop=True)
        samples = draw_joint_usage(players, [game.home_team, game.away_team], residuals, pools, draws=1500, seed=20261014+int(game.week))
        for target in ("rushing_yards", "receiving_yards"):
            valid = population_mask(players, target).to_numpy()
            actual = players.loc[valid, f"actual_{target}"].to_numpy()
            mean = players.loc[valid, f"pred_{target}_champion"].to_numpy()
            z = np.random.default_rng(19).normal(size=(1500, len(actual)))
            methods = {"INCUMBENT_NORMAL": mean+(8+3*np.sqrt(np.maximum(mean,0)))*z,
                "JOINT_CONDITIONAL_EFFICIENCY_RESEARCH": samples[target][:, valid]}
            for name, sim in methods.items():
                lo, hi = np.quantile(sim, [.1,.9], axis=0)
                rows.append({"game_id":game.game_id,"target":target,"method":name,"rows":len(actual),
                    "crps":float(crps_samples(sim,actual).mean()), "coverage80":float(np.mean((actual>=lo)&(actual<=hi))),
                    "width80":float(np.mean(hi-lo)),"mean_deviation_mae":float(np.abs(sim.mean(axis=0)-players.loc[valid,f"pred_{target}"].to_numpy()).mean()),
                    "p99_draw":float(np.quantile(sim,.99)),"count_consistency":bool((samples['receptions']<=samples['targets']).all())})
    pd.DataFrame(rows).to_csv(settings.report_root/"phase7_joint_refinement_backtest.csv",index=False)
    result = {"status":"RESEARCH_ONLY", "games":int(test.game_id.nunique()),"draws":1500,"seconds":time.perf_counter()-started,
        "conditioning":"historical opportunity tertiles, position where >=50 rows; prior 2024 or earlier observations only",
        "same_comparable_populations":True,"promotion":False,
        "remaining":["mean anchoring and threshold calibration", "full TD/scoring coupling", "historical eligibility certification"]}
    json_write(settings.report_root/"phase7_joint_refinement.json",result)
    return result


def allocate_touchdowns(rng, opportunity_counts, player_weights, total_touchdowns):
    """Capacity-constrained offensive TD allocation for future joint integration.

    Each offensive carry/reception can score at most once. Unallocatable TDs are
    returned, never silently fabricated. Caller must supply a validated TD-count
    distribution; an anytime probability is NOT converted into a Poisson rate.
    """
    counts = np.asarray(opportunity_counts, int)
    total_touchdowns = np.asarray(total_touchdowns, int)
    if (counts < 0).any() or (total_touchdowns < 0).any() or counts.ndim != 2 or len(total_touchdowns) != len(counts):
        raise ValueError("TD capacity/count contract invalid")
    result = np.zeros_like(counts)
    remainder = np.zeros(len(counts), int)
    base = np.asarray(player_weights, float)
    weights(base)
    for row, td in enumerate(total_touchdowns):
        capacity = counts[row].copy()
        for _ in range(int(td)):
            w = base * (capacity > 0)
            if w.sum() == 0:
                remainder[row] += 1
                continue
            i = rng.choice(len(base), p=w / w.sum())
            result[row, i] += 1
            capacity[i] -= 1
    return result, remainder


def research_validation(settings):
    path = settings.artifact_root / "research/phase7/player_v4_oos.csv"
    frame = pd.read_csv(path)
    frame = frame[frame.model.eq("phase7-player-v4-opportunity-research-v1-extra_trees")]
    train, test = frame[frame.season.isin([2023, 2024])], frame[frame.season.eq(2025)]
    if train.empty or test.empty or train.max_training_kickoff_utc.isna().any():
        raise ValueError("Genuine OOS calibration/validation folds required")
    if (pd.to_datetime(train.max_training_kickoff_utc, utc=True) >= pd.to_datetime(train.kickoff_utc, utc=True)).any():
        raise ValueError("In-sample or target-game residuals forbidden")
    if pd.to_datetime(train.kickoff_utc, utc=True).max() >= pd.to_datetime(test.kickoff_utc, utc=True).min():
        raise ValueError("Future simulation calibration data")
    games = pd.read_csv(settings.artifact_root / "canonical/canonical_games.csv")
    aggregated = train.groupby(["game_id", "team"])[["pred_carries", "pred_targets", "actual_carries", "actual_targets"]].sum()
    pairs = []
    for g in games[games.game_id.isin(train.game_id)].itertuples():
        if (g.game_id, g.home_team) not in aggregated.index or (g.game_id, g.away_team) not in aggregated.index:
            continue
        values = []
        for team in (g.home_team, g.away_team):
            row = aggregated.loc[(g.game_id, team)]
            values.extend([row.actual_carries-row.pred_carries, row.actual_targets-row.pred_targets])
        pairs.append(values)
    residuals = np.asarray(pairs)
    weekly = pd.read_csv(settings.artifact_root / "canonical/player_weekly.csv", low_memory=False)
    weekly = weekly[weekly.season.le(2024) & weekly.player_id.notna()]
    pools = {}
    for kind, opportunity, yards in (("rush", "carries", "rushing_yards"), ("receive", "receptions", "receiving_yards")):
        valid = weekly[weekly[opportunity].gt(0) & weekly[yards].notna()]
        pools[(kind, "ALL")] = (valid[yards] / valid[opportunity]).to_numpy()
        for position, part in valid.groupby("position"):
            if len(part) >= 30:
                pools[(kind, position)] = (part[yards] / part[opportunity]).to_numpy()
    started = time.perf_counter()
    metrics = []
    structural = True
    for game in games[games.game_id.isin(test.game_id)].itertuples():
        players = test[test.game_id.eq(game.game_id)].reset_index(drop=True)
        samples = draw_joint_usage(players, [game.home_team, game.away_team], residuals, pools,
                                   draws=2000, seed=20261014 + int(game.week))
        structural &= bool((samples["receptions"] <= samples["targets"]).all())
        for side, team in enumerate([game.home_team, game.away_team]):
            ids = players.team.eq(team)
            structural &= bool(np.array_equal(samples["carries"][:, ids].sum(axis=1), samples["team_volume"][:, 2*side]))
            structural &= bool(np.array_equal(samples["targets"][:, ids].sum(axis=1), samples["team_volume"][:, 2*side+1]))
        for field in ("carries", "targets", "receptions", "rushing_yards", "receiving_yards"):
            valid = players[f"actual_{field}"].notna().to_numpy()
            actual = players.loc[valid, f"actual_{field}"].to_numpy()
            sim = samples[field][:, valid]
            lower, upper = np.quantile(sim, [.1, .9], axis=0)
            metrics.append({"game_id": game.game_id, "target": field, "rows": len(actual),
                "crps": float(crps_samples(sim, actual).mean()),
                "coverage80": float(np.mean((actual >= lower) & (actual <= upper))),
                "width80": float(np.mean(upper-lower)),
                "mean_deviation": float(np.mean(sim.mean(axis=0) - players.loc[valid, f"pred_{field}"].to_numpy())),
                "status": "EXPERIMENTAL_UNCERTIFIED_ELIGIBILITY"})
    pd.DataFrame(metrics).to_csv(settings.report_root / "phase7_joint_simulation_backtest.csv", index=False)
    result = {"games": int(test.game_id.nunique()), "draws_per_game": 2000,
        "seconds": time.perf_counter()-started, "structural_consistency": bool(structural),
        "calibration_pair_games": len(residuals), "residual_count_correlation": finite(np.corrcoef(residuals.T).tolist()),
        "promotion": False, "status": "PARTIAL_RESEARCH_PROTOTYPE",
        "limitations": ["historical eligibility uncertified", "efficiency means not anchored/calibrated",
                        "no untargeted pass/sack model", "no validated TD count or score coupling",
                        "no claim of improvement over incumbent uncertainty"]}
    json_write(settings.report_root / "phase7_joint_simulation.json", finite(result))
    return result
