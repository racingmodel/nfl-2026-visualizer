from __future__ import annotations

import numpy as np


def clip_probability(values):
    return np.clip(np.asarray(values, dtype=float), 1e-9, 1 - 1e-9)


def beta_binomial_mean(successes, trials, prior_mean: float, prior_strength: float):
    if not 0 <= prior_mean <= 1 or prior_strength < 0:
        raise ValueError("Invalid beta-binomial prior")
    successes = np.asarray(successes, dtype=float)
    trials = np.asarray(trials, dtype=float)
    if np.any(successes < 0) or np.any(trials < successes):
        raise ValueError("Successes must satisfy 0 <= successes <= trials")
    result = (successes + prior_mean * prior_strength) / (trials + prior_strength)
    return clip_probability(result)


def log_loss_binary(actual, probability) -> float:
    actual = np.asarray(actual, dtype=float)
    probability = clip_probability(probability)
    return float(-np.mean(actual * np.log(probability) + (1 - actual) * np.log(1 - probability)))


def expected_calibration_error(actual, probability, bins: int = 10) -> float:
    actual = np.asarray(actual, dtype=float)
    probability = clip_probability(probability)
    edges = np.linspace(0, 1, bins + 1)
    indices = np.minimum(np.digitize(probability, edges, right=True), bins) - 1
    total = len(actual)
    error = 0.0
    for index in range(bins):
        mask = indices == index
        if mask.any():
            error += mask.mean() * abs(float(actual[mask].mean()) - float(probability[mask].mean()))
    return float(error)
