"""OOS conditional residual experiment. No calibrated production probabilities."""
from __future__ import annotations

import numpy as np
import pandas as pd

from nfl2026.phase7.common import json_write
from nfl2026.phase7.joint import crps_samples
from nfl2026.phase7.player_v4 import KEYS, population_mask
from nfl2026.five_week_review import finite, immutable_research_artifact


def fit_residual_table(oos, target, minimum=100):
    times = pd.to_datetime(oos.kickoff_utc, utc=True)
    training = pd.to_datetime(oos.max_training_kickoff_utc, utc=True)
    if not (training < times).all():
        raise ValueError("Residuals must be genuine out-of-sample predictions")
    frame = oos[population_mask(oos, target) & oos[f"actual_{target}"].notna()].copy()
    if frame.empty:
        raise ValueError("No calibration population")
    mean = frame[f"pred_{target}"]
    edges = np.unique(np.quantile(mean, [0, .25, .5, .75, 1]))[1:-1]
    frame["bucket"] = np.searchsorted(edges, mean)
    frame["residual"] = frame[f"actual_{target}"] - mean
    pools = {"ALL": frame.residual.to_numpy()}
    for position, group in frame.groupby("position"):
        if len(group) >= minimum:
            pools[position] = group.residual.to_numpy()
        for bucket, part in group.groupby("bucket"):
            if len(part) >= minimum:
                pools[f"{position}:{bucket}"] = part.residual.to_numpy()
    return {"target": target, "edges": edges, "pools": pools, "minimum": minimum,
        "support_minimum": min(0., float(frame[f"actual_{target}"].min())),
        "max_calibration_kickoff": times.max().isoformat(), "rows": len(frame)}


def draw_residuals(table, means, positions, *, draws=1000, seed=20261014):
    means = np.asarray(means, float)
    if draws < 1 or means.ndim != 1 or not np.isfinite(means).all() or len(means) != len(positions):
        raise ValueError("Distribution input contract invalid")
    rng = np.random.default_rng(seed)
    result = np.empty((draws, len(means)))
    for i, (mean, position) in enumerate(zip(means, positions)):
        bucket = np.searchsorted(table["edges"], mean)
        pool = table["pools"].get(f"{position}:{bucket}", table["pools"].get(position, table["pools"]["ALL"]))
        result[:, i] = np.maximum(table["support_minimum"], np.rint(mean + rng.choice(pool, draws)))
    return result


def evaluate_distributions(settings):
    source = settings.artifact_root / "research/phase7/player_v4_oos.csv"
    all_oos = pd.read_csv(source)
    all_oos = all_oos[all_oos.model.eq("phase7-player-v4-opportunity-research-v1-extra_trees")]
    train, test = all_oos[all_oos.season.le(2024)], all_oos[all_oos.season.eq(2025)]
    if pd.to_datetime(train.kickoff_utc, utc=True).max() >= pd.to_datetime(test.kickoff_utc, utc=True).min():
        raise ValueError("Target fold entered uncertainty calibration")
    incumbent = pd.read_csv(settings.artifact_root / "research/five_week_review/player_production_semantics_replay.csv")
    test = test.merge(incumbent[KEYS + ["pred_rushing_yards", "pred_receiving_yards"]], on=KEYS,
        validate="one_to_one", suffixes=("", "_incumbent"))
    rows, reliability = [], []
    tables = {}
    for target in ("rushing_yards", "receiving_yards", "targets"):
        tables[target] = fit_residual_table(train, target)
        part = test[population_mask(test, target)].copy().reset_index(drop=True)
        actual = part[f"actual_{target}"].to_numpy()
        means = part[f"pred_{target}"].to_numpy()
        samples = draw_residuals(tables[target], means, part.position, draws=1000)
        methods = {"V4_CONDITIONAL_RESIDUAL_RESEARCH": samples}
        if target.endswith("yards"):
            # Isolate variance-method changes as well as the full challenger.
            z = np.random.default_rng(73).normal(size=samples.shape)
            methods["V4_MEAN_HEURISTIC"] = means + (8+3*np.sqrt(np.maximum(means, 0))) * z
            incumbent_means = part[f"pred_{target}_incumbent"].to_numpy()
            methods["INCUMBENT_HEURISTIC"] = incumbent_means + (8+3*np.sqrt(np.maximum(incumbent_means, 0))) * z
        else:
            methods["V4_POISSON_RESEARCH"] = np.random.default_rng(73).poisson(np.maximum(means, 0), size=samples.shape)
            # Receptions are bounded by sampled targets, not independent counts.
            catches = np.random.default_rng(79).binomial(samples.astype(int), part.catch_probability.to_numpy())
            actual_catches = part.actual_receptions.to_numpy()
            valid = np.isfinite(actual_catches)
            rows.append({"target": "receptions", "method": "TARGET_COUNT_BINOMIAL_RESEARCH", "position": "ALL", "rows": int(valid.sum()),
                "crps": float(crps_samples(catches[:, valid], actual_catches[valid]).mean()),
                "zero_mass": float((catches == 0).mean()), "targets_count_consistency": bool((catches <= samples).all())})
            for threshold in (2.5, 3.5, 4.5, 5.5, 6.5):
                p = (catches[:, valid] > threshold).mean(axis=0)
                rows.append({"target": "receptions", "method": "TARGET_COUNT_BINOMIAL_RESEARCH", "threshold": threshold,
                    "rows": int(valid.sum()), "brier": float(np.mean((p-(actual_catches[valid] > threshold))**2))})
        for name, sim in methods.items():
            for position in ["ALL"] + sorted(part.position.unique().tolist()):
                mask = np.ones(len(part), bool) if position == "ALL" else part.position.eq(position).to_numpy()
                d, observed = sim[:, mask], actual[mask]
                row = {"target": target, "method": name, "position": position, "rows": int(mask.sum()),
                    "crps": float(crps_samples(d, observed).mean()), "zero_mass": float((d == 0).mean()),
                    "mean_deviation": float((d.mean(axis=0)-means[mask]).mean()),
                    "mean_deviation_mae": float(np.abs(d.mean(axis=0)-means[mask]).mean()),
                    "p95_mean_deviation": float(np.quantile(np.abs(d.mean(axis=0)-means[mask]), .95)),
                    "p99_predicted_yards": float(np.quantile(d, .99))}
                for coverage in (.5, .8, .9):
                    lo, hi = np.quantile(d, [(1-coverage)/2, (1+coverage)/2], axis=0)
                    row[f"coverage{int(coverage*100)}"] = float(np.mean((observed >= lo) & (observed <= hi)))
                    row[f"width{int(coverage*100)}"] = float(np.mean(hi-lo))
                # Exact <= nominal quantile differs for discrete zero-heavy data;
                # report it, do not claim equality is achievable at the atom.
                for q in (.1, .25, .5, .75, .9):
                    row[f"cdf_at_q{int(q*100)}"] = float(np.mean(observed <= np.quantile(d, q, axis=0)))
                rows.append(row)
            thresholds = (29.5, 39.5, 49.5, 59.5, 69.5, 79.5, 99.5) if target.endswith("yards") else (2.5, 4.5, 6.5, 8.5)
            for threshold in thresholds:
                prob = (sim > threshold).mean(axis=0)
                outcome = actual > threshold
                rows.append({"target": target, "method": name, "threshold": threshold, "rows": len(part),
                    "brier": float(np.mean((prob-outcome)**2))})
                for low in np.arange(0, 1, .1):
                    mask = (prob >= low) & (prob < low+.1 if low < .9 else prob <= 1)
                    reliability.append({"target": target, "method": name, "threshold": threshold, "bucket": float(low), "rows": int(mask.sum()),
                        "predicted": float(prob[mask].mean()) if mask.any() else None,
                        "observed": float(outcome[mask].mean()) if mask.any() else None})
    immutable_research_artifact(settings.artifact_root / "research/phase7/models/conditional-residual-research-v1.joblib", tables,
        {"training_seasons": [2023, 2024], "method": "position/prediction-quartile OOS residuals, minimum100 fallback",
         "calibration_source_sha256": __import__('nfl2026.utilities.io', fromlist=['sha256_file']).sha256_file(source),
         "max_calibration_kickoff": pd.to_datetime(train.kickoff_utc, utc=True).max().isoformat(), "production_registered": False})
    pd.DataFrame(rows).to_csv(settings.report_root / "phase7_threshold_distribution_backtest.csv", index=False)
    pd.DataFrame(reliability).to_csv(settings.report_root / "phase7_threshold_reliability.csv", index=False)
    summary = {"calibration_seasons": [2023, 2024], "evaluation_season": 2025, "player_rows": len(test),
        "state": "RESEARCH_ONLY_NOT_CERTIFIED", "promotion": False,
        "limitations": ["historical eligibility uncertified", "one repeatedly examined validation fold",
            "support flooring shifts simulated means; base point forecasts unchanged", "independent marginal yards, not full joint team reconciliation",
            "negative observed yardage retained, nonnegative counts", "reception count draws always <= targets",
            "no claim that new probabilities are calibrated for deployment"]}
    json_write(settings.report_root / "phase7_threshold_distribution_summary.json", finite(summary))
    return summary
