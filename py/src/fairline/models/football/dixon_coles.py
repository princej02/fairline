"""Dixon-Coles (1997) bivariate Poisson with low-score correction, fitted by penalised weighted MLE.

    log lambda_home = mu_d + home_d + att_i - def_j
    log lambda_away = mu_d          + att_j - def_i

d is the division of the match, so in a pooled country fit (top + second tier) each division
has its own scoring rate and home advantage while team strengths share one scale. That scale is
linked across tiers by clubs that move between them, which is what anchors promoted teams.

A Gaussian prior N(0, prior_sd) on attack/defence acts as ridge shrinkage and removes the
"add c to every attack and defence" null direction, so no sum-to-zero constraint is needed.

This MLE is the workhorse for walk-forward backtests (a fit takes well under a second).
Uncertainty bands come from the PyMC version in bayes.py, used for the live slate only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import polars as pl
from scipy.optimize import minimize

from fairline.core.config import DixonColesSettings

TAU_FLOOR = 1e-10


@dataclass
class FitData:
    home: np.ndarray  # team index
    away: np.ndarray
    div: np.ndarray  # division index
    hg: np.ndarray
    ag: np.ndarray
    w: np.ndarray  # time-decay weights
    n_teams: int
    n_div: int
    group: np.ndarray | None = None  # per team: division whose mean it is shrunk towards


@dataclass
class DixonColesParams:
    teams: list[int]  # participant ids, in index order
    divisions: list[str]
    attack: np.ndarray
    defence: np.ndarray
    mu: np.ndarray  # per division
    home_adv: np.ndarray  # per division
    rho: float
    effective_matches: np.ndarray  # sum of decay weights per team
    last_division: list[str]  # division of each team's most recent match
    as_of: datetime
    n_matches: int
    loglik: float
    converged: bool
    _index: dict[int, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._index = {t: i for i, t in enumerate(self.teams)}

    def team_index(self, team_id: int) -> int | None:
        return self._index.get(team_id)

    def to_json(self) -> dict:
        return {
            "divisions": self.divisions,
            "mu": dict(zip(self.divisions, self.mu.round(6).tolist(), strict=True)),
            "home_adv": dict(zip(self.divisions, self.home_adv.round(6).tolist(), strict=True)),
            "rho": round(self.rho, 6),
            "teams": {
                str(t): {
                    "attack": round(float(self.attack[i]), 6),
                    "defence": round(float(self.defence[i]), 6),
                    "effective_matches": round(float(self.effective_matches[i]), 3),
                    "last_division": self.last_division[i],
                }
                for i, t in enumerate(self.teams)
            },
            "n_matches": self.n_matches,
            "loglik": self.loglik,
            "converged": self.converged,
        }


# --------------------------------------------------------------------------------------
# Likelihood
# --------------------------------------------------------------------------------------


OFFSET_SD = 1.0  # weak prior on division-level strength offsets


def n_params(n: int, d: int) -> int:
    return 2 * n + 4 * d + 1


def _unpack(theta: np.ndarray, n: int, d: int):
    """att, def (per team), mu, home (per division), c_att, c_def (per-division strength means), rho."""
    k = 2 * n
    return (
        theta[:n], theta[n:k], theta[k : k + d], theta[k + d : k + 2 * d],
        theta[k + 2 * d : k + 3 * d], theta[k + 3 * d : k + 4 * d], theta[-1],
    )  # fmt: skip


def _tau_terms(x, y, lam, m, rho):
    """log tau and its derivatives w.r.t. eta_home, eta_away (log-rates) and rho."""
    tau = np.ones_like(lam)
    dh = np.zeros_like(lam)
    da = np.zeros_like(lam)
    dr = np.zeros_like(lam)

    s00 = (x == 0) & (y == 0)
    s01 = (x == 0) & (y == 1)
    s10 = (x == 1) & (y == 0)
    s11 = (x == 1) & (y == 1)

    lm = lam * m
    tau[s00] = 1 - lm[s00] * rho
    tau[s01] = 1 + lam[s01] * rho
    tau[s10] = 1 + m[s10] * rho
    tau[s11] = 1 - rho
    tau = np.maximum(tau, TAU_FLOOR)

    dh[s00] = -lm[s00] * rho / tau[s00]
    da[s00] = -lm[s00] * rho / tau[s00]
    dr[s00] = -lm[s00] / tau[s00]
    dh[s01] = lam[s01] * rho / tau[s01]
    dr[s01] = lam[s01] / tau[s01]
    da[s10] = m[s10] * rho / tau[s10]
    dr[s10] = m[s10] / tau[s10]
    dr[s11] = -1 / tau[s11]
    return np.log(tau), dh, da, dr


def neg_log_posterior(theta: np.ndarray, data: FitData, prior_sd: float) -> tuple[float, np.ndarray]:
    """Weighted negative log-likelihood + Gaussian prior on att/def, divided by total weight.
    Returns (value, gradient)."""
    n, d = data.n_teams, data.n_div
    att, dfn, mu, home, c_att, c_def, rho = _unpack(theta, n, d)
    h, a, dv = data.home, data.away, data.div
    grp = data.group if data.group is not None else np.zeros(n, dtype=np.int64)

    eta_h = mu[dv] + home[dv] + att[h] - dfn[a]
    eta_a = mu[dv] + att[a] - dfn[h]
    lam, m = np.exp(eta_h), np.exp(eta_a)
    log_tau, dth, dta, dtr = _tau_terms(data.hg, data.ag, lam, m, rho)

    w = data.w
    ll = w * (data.hg * eta_h - lam + data.ag * eta_a - m + log_tau)
    # Hierarchical shrinkage: each team towards its division's mean strength, so a pooled fit
    # does not squeeze the gap between tiers (which overrates promoted clubs).
    inv_var = 1.0 / prior_sd**2
    ra, rd = att - c_att[grp], dfn - c_def[grp]
    inv_off = 1.0 / OFFSET_SD**2
    penalty = 0.5 * inv_var * (ra @ ra + rd @ rd) + 0.5 * inv_off * (c_att @ c_att + c_def @ c_def)
    W = w.sum()
    value = -(ll.sum() - penalty) / W

    gh = w * (data.hg - lam + dth)
    ga = w * (data.ag - m + dta)
    g_att = np.bincount(h, gh, n) + np.bincount(a, ga, n) - inv_var * ra
    g_def = -np.bincount(a, gh, n) - np.bincount(h, ga, n) - inv_var * rd
    g_mu = np.bincount(dv, gh + ga, d)
    g_home = np.bincount(dv, gh, d)
    g_catt = inv_var * np.bincount(grp, ra, d) - inv_off * c_att
    g_cdef = inv_var * np.bincount(grp, rd, d) - inv_off * c_def
    g_rho = (w * dtr).sum()
    grad = -np.concatenate([g_att, g_def, g_mu, g_home, g_catt, g_cdef, [g_rho]]) / W
    return value, grad


# --------------------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------------------


def training_frame(matches: pl.DataFrame, as_of: datetime, s: DixonColesSettings) -> pl.DataFrame:
    """Matches whose result was knowable at `as_of`, with time-decay weights. The single
    place the anti-leakage cutoff is applied."""
    known_by = as_of - timedelta(hours=s.result_delay_hours)
    oldest = as_of - timedelta(days=s.max_history_days)
    return (
        matches.filter(
            pl.col("home_goals").is_not_null()
            & pl.col("away_goals").is_not_null()
            & (pl.col("starts_at") <= known_by)
            & (pl.col("starts_at") >= oldest)
        )
        .with_columns(
            ((pl.lit(as_of) - pl.col("starts_at")).dt.total_seconds() / 86400.0).alias("age_days"),
        )
        .with_columns((0.5 ** (pl.col("age_days") / s.half_life_days)).alias("weight"))
        .sort("starts_at")
    )


def fit(
    matches: pl.DataFrame,
    as_of: datetime,
    s: DixonColesSettings,
    init: DixonColesParams | None = None,
) -> DixonColesParams:
    """Fit on everything in `matches` that was known at `as_of`.

    `matches` needs: starts_at, competition, home_id, away_id, home_goals, away_goals.
    `init` warm-starts from a previous fit (matched by team id / division), which makes
    weekly walk-forward refits converge in a few iterations.
    """
    train = training_frame(matches, as_of, s)
    if train.height < 50:
        raise ValueError(f"only {train.height} training matches before {as_of:%Y-%m-%d}; need >= 50")

    teams = sorted(set(train["home_id"].to_list()) | set(train["away_id"].to_list()))
    divisions = sorted(set(train["competition"].to_list()))
    t_idx = {t: i for i, t in enumerate(teams)}
    d_idx = {c: i for i, c in enumerate(divisions)}

    last_div = {}
    for row in train.select("home_id", "away_id", "competition").iter_rows():
        last_div[row[0]] = row[2]
        last_div[row[1]] = row[2]

    data = FitData(
        home=np.array([t_idx[t] for t in train["home_id"]], dtype=np.int64),
        away=np.array([t_idx[t] for t in train["away_id"]], dtype=np.int64),
        div=np.array([d_idx[c] for c in train["competition"]], dtype=np.int64),
        hg=train["home_goals"].to_numpy().astype(np.float64),
        ag=train["away_goals"].to_numpy().astype(np.float64),
        w=train["weight"].to_numpy(),
        n_teams=len(teams),
        n_div=len(divisions),
        group=np.array([d_idx[last_div[t]] for t in teams], dtype=np.int64),
    )
    n, d = data.n_teams, data.n_div

    theta0 = np.zeros(n_params(n, d))
    mean_goals = np.average((data.hg + data.ag) / 2, weights=data.w)
    theta0[2 * n : 2 * n + d] = np.log(max(mean_goals, 0.1))
    theta0[2 * n + d : 2 * n + 2 * d] = 0.25
    theta0[-1] = -0.05
    if init is not None:
        for i, t in enumerate(teams):
            j = init.team_index(t)
            if j is not None:
                theta0[i], theta0[n + i] = init.attack[j], init.defence[j]
        for k, c in enumerate(divisions):
            if c in init.divisions:
                j = init.divisions.index(c)
                theta0[2 * n + k], theta0[2 * n + d + k] = init.mu[j], init.home_adv[j]
        theta0[-1] = init.rho

    bounds = [(None, None)] * (n_params(n, d) - 1) + [tuple(s.rho_bounds)]
    res = minimize(
        neg_log_posterior,
        theta0,
        args=(data, s.prior_sd),
        jac=True,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 2000, "gtol": 1e-7},
    )
    att, dfn, mu, home, _, _, rho = _unpack(res.x, n, d)

    eff = np.bincount(data.home, data.w, n) + np.bincount(data.away, data.w, n)

    return DixonColesParams(
        teams=teams,
        divisions=divisions,
        attack=att.copy(),
        defence=dfn.copy(),
        mu=mu.copy(),
        home_adv=home.copy(),
        rho=float(rho),
        effective_matches=eff,
        last_division=[last_div[t] for t in teams],
        as_of=as_of,
        n_matches=train.height,
        loglik=float(-res.fun * data.w.sum()),
        converged=bool(res.success),
    )


# --------------------------------------------------------------------------------------
# Ratings with cold-start fallback
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamRating:
    attack: float
    defence: float
    cold_start: bool  # True if the prior was used instead of fitted values


def cold_start_prior(p: DixonColesParams, division: str, s: DixonColesSettings) -> TeamRating:
    """Bottom-quantile attack and defence among established teams last seen in `division`.
    A promoted club with no usable history is assumed to be a weak team in its new league."""
    est = p.effective_matches >= s.min_effective_matches
    in_div = np.array([ld == division for ld in p.last_division]) & est
    pool = in_div if in_div.sum() >= 4 else est
    q = s.promoted_prior_quantile
    return TeamRating(float(np.quantile(p.attack[pool], q)), float(np.quantile(p.defence[pool], q)), True)


def team_rating(p: DixonColesParams, team_id: int, division: str, s: DixonColesSettings) -> TeamRating:
    i = p.team_index(team_id)
    if i is None or p.effective_matches[i] < s.min_effective_matches:
        return cold_start_prior(p, division, s)
    return TeamRating(float(p.attack[i]), float(p.defence[i]), False)


def expected_goals(
    p: DixonColesParams, home_id: int, away_id: int, division: str, s: DixonColesSettings
) -> tuple[float, float, bool, bool]:
    """(lambda_home, lambda_away, home_cold_start, away_cold_start) for a match in `division`."""
    if division not in p.divisions:
        raise ValueError(f"division {division} not in fit {p.divisions}")
    k = p.divisions.index(division)
    h = team_rating(p, home_id, division, s)
    a = team_rating(p, away_id, division, s)
    lam = float(np.exp(p.mu[k] + p.home_adv[k] + h.attack - a.defence))
    mu = float(np.exp(p.mu[k] + a.attack - h.defence))
    return lam, mu, h.cold_start, a.cold_start
