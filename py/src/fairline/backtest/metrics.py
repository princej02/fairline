"""Scoring and resampling helpers for backtests.

Uncertainty: bets on the same matchday are not independent (shared news, weather, correlated
markets), so every confidence interval uses a *cluster* bootstrap that resamples whole
matchdays, not individual bets.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS = 1e-12


def log_loss(p_outcome: np.ndarray) -> np.ndarray:
    """Per-match log loss given the probability assigned to what actually happened."""
    return -np.log(np.clip(p_outcome, EPS, 1.0))


def brier(probs: np.ndarray, outcome_idx: np.ndarray) -> np.ndarray:
    """Per-match multi-class Brier score. probs: (n, k); outcome_idx: (n,)."""
    onehot = np.zeros_like(probs)
    onehot[np.arange(len(probs)), outcome_idx] = 1.0
    return ((probs - onehot) ** 2).sum(axis=1)


def wilson(successes: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    ph = successes / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * np.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (float(c - h), float(c + h))


@dataclass(frozen=True)
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_p: float
    observed: float
    ci_lo: float
    ci_hi: float

    @property
    def consistent(self) -> bool:
        """Observed frequency's 95% Wilson interval contains the mean prediction."""
        return self.ci_lo <= self.mean_p <= self.ci_hi


def calibration(p: np.ndarray, y: np.ndarray, edges: np.ndarray | None = None) -> list[CalibrationBin]:
    edges = np.linspace(0, 1, 11) if edges is None else edges
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        n = int(m.sum())
        if n == 0:
            continue
        k = float(y[m].sum())
        out.append(CalibrationBin(float(lo), float(hi), n, float(p[m].mean()), k / n, *wilson(k, n)))
    return out


def cluster_bootstrap(
    values: np.ndarray, clusters: np.ndarray, stat=np.mean, n_boot: int = 2000, seed: int = 0, weights=None
) -> tuple[float, float, float]:
    """(estimate, 2.5%, 97.5%) of stat(values), resampling whole clusters with replacement.
    With weights, the statistic is sum(w * v) / sum(w) (e.g. stake-weighted ROI)."""
    values = np.asarray(values, float)
    if values.size == 0:
        return (float("nan"),) * 3
    uniq, inv = np.unique(clusters, return_inverse=True)
    w = np.ones_like(values) if weights is None else np.asarray(weights, float)
    sum_v = np.bincount(inv, w * values, len(uniq))
    sum_w = np.bincount(inv, w, len(uniq))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    if stat is np.mean:
        boots = sum_v[idx].sum(1) / np.maximum(sum_w[idx].sum(1), EPS)
        est = sum_v.sum() / max(sum_w.sum(), EPS)
    else:  # generic (slower) path
        groups = [values[inv == g] for g in range(len(uniq))]
        boots = np.array([stat(np.concatenate([groups[g] for g in row])) for row in idx])
        est = stat(values)
    lo, hi = np.quantile(boots, [0.025, 0.975])
    return float(est), float(lo), float(hi)


def max_drawdown(bankroll: np.ndarray) -> float:
    """Largest peak-to-trough fall as a fraction of the peak (0.25 = -25%)."""
    if bankroll.size == 0:
        return 0.0
    peak = np.maximum.accumulate(bankroll)
    return float(((peak - bankroll) / peak).max())
