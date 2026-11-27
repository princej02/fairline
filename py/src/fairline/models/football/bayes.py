"""Bayesian Dixon-Coles in PyMC, used only for the live slate's uncertainty bands.

The MLE in dixon_coles.py drives every walk-forward backtest. Full MCMC inside a weekly
walk-forward over several seasons would take hours, so this model runs once per slate on
a shorter window (models.bayes.history_days) and returns posterior mean plus a central
credible interval (prob_lo / prob_hi) for each market selection.

Same likelihood and time-decay weights as the MLE. Attack and defence are ZeroSumNormal deviations
with a per-division mean (the same hierarchy as the MLE); the top division's offset is
pinned at 0 to remove the shift null direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import polars as pl
import pymc as pm
import pytensor.tensor as pt

from fairline.core.config import BayesSettings, DixonColesSettings
from fairline.models.football import dixon_coles as dc
from fairline.models.football.markets import total_distribution
from fairline.models.football.scores import score_matrices


@dataclass
class BayesResult:
    predictions: pl.DataFrame  # event_id, market, line, selection, prob, prob_lo, prob_hi
    max_rhat: float | None
    divergences: int
    n_matches: int
    idata: object = field(default=None, repr=False)  # full sampler output, for diagnostics
    healthy: bool = True  # False => flag the slate's bands as unreliable

    # Thresholds: a handful of divergences per 4,000 draws is normal for this model (they occur
    # where a strongly negative rho drives the low-score correction towards 0); more is not.
    MAX_DIVERGENCE_RATE = 0.005
    MAX_RHAT = 1.01


def _posterior(idata):
    return idata.posterior if hasattr(idata, "posterior") else idata["posterior"]


def fit_and_predict(
    matches: pl.DataFrame,
    fixtures: pl.DataFrame,
    as_of: datetime,
    s: DixonColesSettings,
    b: BayesSettings,
    random_seed: int = 7,
    max_predictive_draws: int = 1000,
) -> BayesResult:
    s_window = replace_history(s, b.history_days)
    train = dc.training_frame(matches, as_of, s_window)
    if train.height < 50:
        raise ValueError(f"only {train.height} training matches before {as_of:%Y-%m-%d}")

    teams = sorted(set(train["home_id"].to_list()) | set(train["away_id"].to_list()))
    divisions = sorted(set(train["competition"].to_list()))
    t_idx = {t: i for i, t in enumerate(teams)}
    d_idx = {c: i for i, c in enumerate(divisions)}
    h = np.array([t_idx[t] for t in train["home_id"]])
    a = np.array([t_idx[t] for t in train["away_id"]])
    dv = np.array([d_idx[c] for c in train["competition"]])
    x = train["home_goals"].to_numpy().astype(float)
    y = train["away_goals"].to_numpy().astype(float)
    w = train["weight"].to_numpy()

    last_div: dict[int, str] = {}
    for hid, aid, comp in train.select("home_id", "away_id", "competition").iter_rows():
        last_div[hid] = comp
        last_div[aid] = comp
    grp = np.array([d_idx[last_div[t]] for t in teams])

    with pm.Model():
        # Team = division mean + deviation that sums to zero *within* its division (same hierarchy
        # as the MLE). The offset is then exactly the division mean, so it doesn't trade off
        # against the deviations; the first division is pinned at 0 to fix the overall level.
        n_d = len(divisions)
        off_a = pm.Normal("off_att", 0, dc.OFFSET_SD, shape=n_d - 1) if n_d > 1 else pt.zeros(0)
        off_d = pm.Normal("off_def", 0, dc.OFFSET_SD, shape=n_d - 1) if n_d > 1 else pt.zeros(0)
        ca = pt.concatenate([pt.zeros(1), off_a])
        cd = pt.concatenate([pt.zeros(1), off_d])
        att_t, def_t = pt.zeros(len(teams)), pt.zeros(len(teams))
        for k in range(n_d):
            idx = np.flatnonzero(grp == k)
            if idx.size == 0:
                continue
            if idx.size == 1:
                att_t = pt.set_subtensor(att_t[idx], ca[k])
                def_t = pt.set_subtensor(def_t[idx], cd[k])
                continue
            dev_a = pm.ZeroSumNormal(f"att_dev_{k}", sigma=s.prior_sd, shape=idx.size)
            dev_d = pm.ZeroSumNormal(f"def_dev_{k}", sigma=s.prior_sd, shape=idx.size)
            att_t = pt.set_subtensor(att_t[idx], ca[k] + dev_a)
            def_t = pt.set_subtensor(def_t[idx], cd[k] + dev_d)
        att = pm.Deterministic("att", att_t)
        dfn = pm.Deterministic("def", def_t)
        mu = pm.Normal("mu", np.log(1.35), 0.5, shape=len(divisions))
        home = pm.Normal("home", 0.25, 0.25, shape=len(divisions))
        rho = pm.Uniform("rho", *s.rho_bounds)

        eta_h = mu[dv] + home[dv] + att[h] - dfn[a]
        eta_a = mu[dv] + att[a] - dfn[h]
        lam, m = pt.exp(eta_h), pt.exp(eta_a)
        tau = pt.switch(
            (x == 0) & (y == 0), 1 - lam * m * rho,
            pt.switch((x == 0) & (y == 1), 1 + lam * rho,
            pt.switch((x == 1) & (y == 0), 1 + m * rho,
            pt.switch((x == 1) & (y == 1), 1 - rho, 1.0))),
        )  # fmt: skip
        ll = x * eta_h - lam + y * eta_a - m + pt.log(pt.maximum(tau, dc.TAU_FLOOR))
        pm.Potential("weighted_loglik", (w * ll).sum())

        idata = pm.sample(
            draws=b.draws, tune=b.tune, chains=b.chains, cores=b.chains,
            random_seed=random_seed, mp_ctx="forkserver", progressbar=False, target_accept=b.target_accept,
        )  # fmt: skip

    post = _posterior(idata)
    flat = {k: np.asarray(post[k]).reshape(-1, *np.asarray(post[k]).shape[2:]) for k in ("att", "def", "mu", "home")}
    rho_d = np.asarray(post["rho"]).reshape(-1)
    rng = np.random.default_rng(random_seed)
    pick = rng.choice(rho_d.size, size=min(max_predictive_draws, rho_d.size), replace=False)
    A, D, MU, HO, R = flat["att"][pick], flat["def"][pick], flat["mu"][pick], flat["home"][pick], rho_d[pick]

    # Per-draw cold-start prior: bottom quantile within the division, as in the MLE.
    eff = np.bincount(h, w, len(teams)) + np.bincount(a, w, len(teams))

    def team_draws(team_id: int, division: str) -> tuple[np.ndarray, np.ndarray]:
        i = t_idx.get(team_id)
        if i is not None and eff[i] >= s.min_effective_matches:
            return A[:, i], D[:, i]
        est = eff >= s.min_effective_matches
        mask = np.array([last_div[t] == division for t in teams]) & est
        mask = mask if mask.sum() >= 4 else est
        q = s.promoted_prior_quantile
        return np.quantile(A[:, mask], q, axis=1), np.quantile(D[:, mask], q, axis=1)

    lo_q, hi_q = (1 - b.interval) / 2, 1 - (1 - b.interval) / 2
    rows = []
    for ev in fixtures.iter_rows(named=True):
        k = d_idx[ev["competition"]]
        ah, dh = team_draws(ev["home_id"], ev["competition"])
        aa, da = team_draws(ev["away_id"], ev["competition"])
        lam_d = np.exp(MU[:, k] + HO[:, k] + ah - da)
        mu_d = np.exp(MU[:, k] + aa - dh)
        mats = score_matrices(lam_d, mu_d, R, s.max_goals)
        sel = _selection_draws(mats)
        for (market, line, selection), p in sel.items():
            rows.append(
                {
                    "event_id": ev["event_id"], "market": market, "line": line, "selection": selection,
                    "prob": float(p.mean()), "prob_lo": float(np.quantile(p, lo_q)),
                    "prob_hi": float(np.quantile(p, hi_q)),
                }
            )  # fmt: skip

    rhat, n_div = _max_rhat(idata), _divergences(idata)
    healthy = (
        rhat is not None and rhat <= BayesResult.MAX_RHAT
        and 0 <= n_div <= BayesResult.MAX_DIVERGENCE_RATE * b.draws * b.chains
    )  # fmt: skip
    return BayesResult(
        predictions=pl.DataFrame(rows),
        max_rhat=rhat,
        divergences=n_div,
        n_matches=train.height,
        idata=idata,
        healthy=healthy,
    )


def _selection_draws(mats: np.ndarray) -> dict[tuple[str, float | None, str], np.ndarray]:
    home = np.tril(mats, -1).sum(axis=(-2, -1))
    draw = np.trace(mats, axis1=-2, axis2=-1)
    over = np.array([total_distribution(m)[1][3:].sum() for m in mats])
    return {
        ("1X2", None, "home"): home,
        ("1X2", None, "draw"): draw,
        ("1X2", None, "away"): 1 - home - draw,
        ("OU", 2.5, "over"): over,
        ("OU", 2.5, "under"): 1 - over,
    }


def replace_history(s: DixonColesSettings, days: int) -> DixonColesSettings:
    return s.model_copy(update={"max_history_days": days})


def _max_rhat(idata) -> float | None:
    try:
        import arviz as az

        r = az.rhat(idata)
        return float(max(float(np.nanmax(np.asarray(r[v]))) for v in r.data_vars))
    except Exception:
        return None


def _divergences(idata) -> int:
    try:
        ss = idata.sample_stats if hasattr(idata, "sample_stats") else idata["sample_stats"]
        return int(np.asarray(ss["diverging"]).sum())
    except Exception:
        return -1
