"""Research-only empirical yardage distributions fitted to prior OOS residuals.

This module does not alter the registered player mean models.  A calibration
bucket is selected using only predicted opportunity and pregame position.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
MIN_BUCKET = 150


def heuristic_sd(mean: np.ndarray) -> np.ndarray:
    return 8.0 + 3.0 * np.sqrt(np.maximum(np.asarray(mean, dtype=float), 0.0))


def heuristic_quantiles(mean: np.ndarray) -> np.ndarray:
    mean = np.asarray(mean, dtype=float)
    return np.maximum(0.0, mean[:, None] + heuristic_sd(mean)[:, None] * norm.ppf(QUANTILES)[None, :])


def heuristic_probability_over(mean: np.ndarray, line: float) -> np.ndarray:
    return norm.sf((line - np.asarray(mean, dtype=float)) / heuristic_sd(mean))


def heuristic_crps(mean: np.ndarray, actual: np.ndarray) -> np.ndarray:
    """CRPS of the actual, untruncated Normal CDF used by market comparison."""
    sd = heuristic_sd(mean)
    z = (np.asarray(actual, dtype=float) - np.asarray(mean, dtype=float)) / sd
    return sd * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / np.sqrt(np.pi))


@dataclass(frozen=True)
class ResidualBucket:
    residuals: np.ndarray
    source: str

    def __post_init__(self) -> None:
        if not len(self.residuals) or not np.all(np.isfinite(self.residuals)):
            raise ValueError("Calibration bucket requires finite prior OOS residuals")
        self.residuals.setflags(write=False)

    def quantiles(self, mean: np.ndarray) -> np.ndarray:
        return np.asarray(mean, dtype=float)[:, None] + np.quantile(self.residuals, QUANTILES)[None, :]

    def probability_over(self, mean: np.ndarray, line: float) -> np.ndarray:
        # Historical yardage is integer. Half-yard thresholds have no push.
        thresholds = line - np.asarray(mean, dtype=float)
        return 1.0 - np.searchsorted(self.residuals, thresholds, side="right") / len(self.residuals)

    def crps(self, mean: np.ndarray, actual: np.ndarray) -> np.ndarray:
        """Exact CRPS of empirical residual CDF, vectorized over predictions."""
        r = self.residuals
        target = np.asarray(actual, dtype=float) - np.asarray(mean, dtype=float)
        k = np.searchsorted(r, target, side="right")
        sums = np.concatenate(([0.0], np.cumsum(r)))
        left = target * k - sums[k]
        right = (sums[-1] - sums[k]) - target * (len(r) - k)
        # 0.5 E|X-X'| = sum_i (2i-n-1)x_i / n^2 for sorted x_i.
        indexes = np.arange(1, len(r) + 1, dtype=float)
        dispersion = np.dot(2 * indexes - len(r) - 1, r) / len(r) ** 2
        return (left + right) / len(r) - dispersion

    def sample(self, mean: float, count: int, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        return float(mean) + rng.choice(self.residuals, size=count, replace=True)


class EmpiricalYardageCalibrator:
    """Calibrates residuals using preceding OOS seasons only.

    Methods are fixed in advance. Conditional buckets with too few examples
    fall back to a coarser prior bucket, never a future-season observation.
    """

    def __init__(self, method: str, min_bucket: int = MIN_BUCKET):
        self.centered = method.endswith("_centered")
        method = method.removesuffix("_centered")
        if method not in {"global", "position", "opportunity", "position_opportunity"}:
            raise ValueError(f"Unknown method: {method}")
        self.method = method
        self.min_bucket = min_bucket
        self.buckets: dict[str, ResidualBucket] = {}
        self.edges = np.array([], dtype=float)
        self.training_seasons: tuple[int, ...] = ()

    @staticmethod
    def position_group(position: str, market: str) -> str:
        if market == "rush":
            return "RB_FB" if position in {"RB", "FB"} else "QB" if position == "QB" else "OTHER"
        return position if position in {"WR", "TE", "RB"} else "OTHER"

    def _opportunity_group(self, opportunity: float) -> str:
        if opportunity <= 0:
            return "zero"
        return str(int(np.searchsorted(self.edges, opportunity, side="right")))

    def fit(self, frame, market: str):
        if frame.empty:
            raise ValueError("No prior OOS predictions")
        if frame.season.nunique() < 1:
            raise ValueError("Missing OOS seasons")
        self.training_seasons = tuple(sorted(frame.season.astype(int).unique()))
        values = frame[f"pred_{'carries' if market == 'rush' else 'targets'}"].to_numpy(dtype=float)
        positive = values[values > 0]
        self.edges = np.unique(np.quantile(positive, [0.25, 0.5, 0.75])) if len(positive) else np.array([])
        residual = frame[f"actual_{'rushing_yards' if market == 'rush' else 'receiving_yards'}"].to_numpy(dtype=float) - frame[f"pred_{'rushing_yards' if market == 'rush' else 'receiving_yards'}"].to_numpy(dtype=float)
        position = frame.position.astype(str).map(lambda x: self.position_group(x, market)).to_numpy()
        opportunity = np.array([self._opportunity_group(x) for x in values])
        def fitted(values: np.ndarray, source: str) -> ResidualBucket:
            return ResidualBucket(np.sort(values - values.mean() if self.centered else values), source)

        self.buckets = {"global": fitted(residual, "global")}
        for label in np.unique(position):
            mask = position == label
            if mask.sum() >= self.min_bucket:
                self.buckets[f"position:{label}"] = fitted(residual[mask], f"position:{label}")
        for label in np.unique(opportunity):
            mask = opportunity == label
            if mask.sum() >= self.min_bucket:
                self.buckets[f"opportunity:{label}"] = fitted(residual[mask], f"opportunity:{label}")
        for p in np.unique(position):
            for o in np.unique(opportunity):
                mask = (position == p) & (opportunity == o)
                if mask.sum() >= self.min_bucket:
                    key = f"position_opportunity:{p}:{o}"
                    self.buckets[key] = fitted(residual[mask], key)
        return self

    def bucket(self, position: str, opportunity: float, market: str) -> ResidualBucket:
        p = self.position_group(position, market)
        o = self._opportunity_group(float(opportunity))
        if self.method == "position_opportunity":
            keys = (f"position_opportunity:{p}:{o}", f"position:{p}", f"opportunity:{o}", "global")
        elif self.method == "position":
            keys = (f"position:{p}", "global")
        elif self.method == "opportunity":
            keys = (f"opportunity:{o}", "global")
        else:
            keys = ("global",)
        return next(self.buckets[key] for key in keys if key in self.buckets)
