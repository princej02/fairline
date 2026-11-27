"""Score matrix: P(home = i, away = j) on a 0..max_goals grid with the Dixon-Coles correction."""

from __future__ import annotations

import numpy as np
from scipy.stats import poisson

MIN_GOALS = 10


def score_matrix(lam: float, mu: float, rho: float, max_goals: int = MIN_GOALS) -> np.ndarray:
    """(max_goals+1) x (max_goals+1) matrix, rows = home goals, cols = away goals.

    The tau correction preserves total mass on an infinite grid, but truncation drops the
    tail, so the matrix is renormalised to sum to exactly 1 before any market is derived.
    """
    if max_goals < MIN_GOALS:
        raise ValueError(f"max_goals must be >= {MIN_GOALS} (got {max_goals})")
    g = np.arange(max_goals + 1)
    m = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    m[0, 0] *= max(1 - lam * mu * rho, 0.0)
    m[0, 1] *= max(1 + lam * rho, 0.0)
    m[1, 0] *= max(1 + mu * rho, 0.0)
    m[1, 1] *= max(1 - rho, 0.0)
    return m / m.sum()


def score_matrices(lam: np.ndarray, mu: np.ndarray, rho: np.ndarray, max_goals: int = MIN_GOALS) -> np.ndarray:
    """Vectorised version: inputs broadcast to shape S, output S + (G, G). Used for posterior draws."""
    if max_goals < MIN_GOALS:
        raise ValueError(f"max_goals must be >= {MIN_GOALS} (got {max_goals})")
    lam, mu, rho = np.broadcast_arrays(np.asarray(lam, float), np.asarray(mu, float), np.asarray(rho, float))
    g = np.arange(max_goals + 1)
    ph = poisson.pmf(g, lam[..., None])
    pa = poisson.pmf(g, mu[..., None])
    m = ph[..., :, None] * pa[..., None, :]
    m[..., 0, 0] *= np.maximum(1 - lam * mu * rho, 0.0)
    m[..., 0, 1] *= np.maximum(1 + lam * rho, 0.0)
    m[..., 1, 0] *= np.maximum(1 + mu * rho, 0.0)
    m[..., 1, 1] *= np.maximum(1 - rho, 0.0)
    return m / m.sum(axis=(-2, -1), keepdims=True)
