"""Causal three-state Gaussian HMM for regimes, kept dependency-free.

The model is intentionally one-dimensional: rolling drawdown from the recent
high.  It is run in shadow mode until forward statistics show it adds lift over
the heuristic ADX/Keltner regime.
"""
from __future__ import annotations

import math
from typing import Sequence

from .features import clamp


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def _gaussian(x: float, mean: float, var: float) -> float:
    var = max(var, 1e-6)
    return math.exp(-0.5 * ((x - mean) ** 2 / var + math.log(2 * math.pi * var)))


def fit_gaussian_hmm(observations: Sequence[float], iterations: int = 24) -> dict:
    xs = [float(value) for value in observations if math.isfinite(float(value))]
    if len(xs) < 20:
        return {"ok": False, "error": "not enough observations"}
    mean = sum(xs) / len(xs)
    std = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs)) or 1.0
    xs = [(x - mean) / std for x in xs]
    means = [_quantile(xs, 1 / 6), _quantile(xs, 1 / 2), _quantile(xs, 5 / 6)]
    variances = [max(0.08, sum((x - m) ** 2 for x in xs) / len(xs)) for m in means]
    pi = [1 / 3, 1 / 3, 1 / 3]
    transition = [[0.94 if i == j else 0.03 for j in range(3)] for i in range(3)]
    for _ in range(iterations):
        emissions = [[_gaussian(x, means[k], variances[k]) for k in range(3)] for x in xs]
        alpha = []
        scales = []
        for t, row in enumerate(emissions):
            if t == 0:
                current = [pi[k] * row[k] for k in range(3)]
            else:
                prev = alpha[-1]
                current = [
                    sum(prev[i] * transition[i][k] for i in range(3)) * row[k]
                    for k in range(3)
                ]
            scale = sum(current) or 1e-300
            current = [value / scale for value in current]
            alpha.append(current)
            scales.append(scale)
        beta = [[1.0, 1.0, 1.0] for _ in xs]
        for t in range(len(xs) - 2, -1, -1):
            for i in range(3):
                beta[t][i] = sum(
                    transition[i][j] * emissions[t + 1][j] * beta[t + 1][j]
                    for j in range(3)
                )
                beta[t][i] /= scales[t + 1] or 1e-300
        gamma = []
        for t in range(len(xs)):
            denom = sum(alpha[t][i] * beta[t][i] for i in range(3)) or 1e-300
            gamma.append([alpha[t][i] * beta[t][i] / denom for i in range(3)])
        new_pi = gamma[0]
        new_transition = [[0.0] * 3 for _ in range(3)]
        denom_matrix = [0.0] * 3
        for t in range(len(xs) - 1):
            denom = sum(
                alpha[t][i] * transition[i][j] * emissions[t + 1][j] * beta[t + 1][j]
                for i in range(3) for j in range(3)
            ) or 1e-300
            for i in range(3):
                for j in range(3):
                    value = (alpha[t][i] * transition[i][j] *
                             emissions[t + 1][j] * beta[t + 1][j] / denom)
                    new_transition[i][j] += value
                    denom_matrix[i] += value
        for i in range(3):
            for j in range(3):
                new_transition[i][j] /= denom_matrix[i] or 1e-300
        new_means = []
        new_vars = []
        for k in range(3):
            weight = sum(gamma[t][k] for t in range(len(xs))) or 1e-300
            mu = sum(gamma[t][k] * xs[t] for t in range(len(xs))) / weight
            var = sum(gamma[t][k] * (xs[t] - mu) ** 2 for t in range(len(xs))) / weight
            new_means.append(mu)
            new_vars.append(max(var, 1e-4))
        pi, transition, means, variances = new_pi, new_transition, new_means, new_vars
    emissions = [[_gaussian(x, means[k], variances[k]) for k in range(3)] for x in xs]
    alpha = []
    for t, row in enumerate(emissions):
        if t == 0:
            current = [pi[k] * row[k] for k in range(3)]
        else:
            current = [
                sum(alpha[-1][i] * transition[i][k] for i in range(3)) * row[k]
                for k in range(3)
            ]
        scale = sum(current) or 1e-300
        alpha.append([value / scale for value in current])
    order = sorted(range(3), key=lambda k: means[k])
    probs = alpha[-1]
    sorted_probs = [probs[k] for k in order]
    labels = ["bear", "range", "bull"]
    return {
        "ok": True,
        "probabilities": {labels[i]: round(sorted_probs[i], 6) for i in range(3)},
        "means": {labels[i]: round(means[order[i]] * std + mean, 6) for i in range(3)},
        "transition": [[round(value, 6) for value in row] for row in transition],
    }


def drawdown_observations(bars: Sequence, window: int = 180) -> list[float]:
    closes = [float(bar.close) for bar in bars]
    out = []
    for i, close in enumerate(closes):
        start = max(0, i - window + 1)
        peak = max(closes[start:i + 1] or [close])
        out.append(close / peak - 1 if peak > 0 else 0.0)
    return out


def hmm_regime(bars: Sequence, window: int = 180, iterations: int = 24) -> dict:
    observations = drawdown_observations(bars, window=window)
    fit = fit_gaussian_hmm(observations, iterations=iterations)
    if not fit.get("ok"):
        return fit
    probs = fit["probabilities"]
    label = max(probs, key=probs.get)
    fit.update({
        "label": label,
        "confidence": round(clamp(float(probs.get(label) or 0.0)), 6),
    })
    return fit
