"""Check whether the PyMC 90% bands are calibrated, on a sample of historical slates.

An interval on p can't "contain" a 0/1 outcome, so the bands are checked two ways instead:
1. Coverage of a proxy for the true p: the devigged Pinnacle closing probability. If the band
   describes uncertainty about the true p, roughly 90% of closes should fall inside it (a
   bit more is expected, since the close itself is a noisy estimate of p).
2. Binned realised rate: group selections by posterior mean, and check that each bin's
   realised win rate (with a Wilson interval) sits inside the bin's average band, near its
   average posterior mean.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import psycopg

from fairline.backtest import metrics as M
from fairline.backtest.data import closing, outcome
from fairline.core.config import Config
from fairline.models.football.bayes import fit_and_predict
from fairline.models.football.data import load_matches


def run_bands(conn: psycopg.Connection, config: Config, pools: list[str], slates: int, seed: int = 3) -> pl.DataFrame:
    s = config.settings.models.dixon_coles
    b = config.settings.models.bayes
    rng = random.Random(seed)
    rows = []
    for pool in pools:
        matches = load_matches(conn, s.pools[pool])
        played = matches.filter(pl.col("home_goals").is_not_null())
        days = sorted({d for d in played["starts_at"].dt.date().to_list() if d.year >= 2018 and d.weekday() == 5})
        for day in rng.sample(days, min(slates, len(days))):
            t = datetime(day.year, day.month, day.day, tzinfo=UTC)
            fx = played.filter((pl.col("starts_at") >= t) & (pl.col("starts_at") < t + timedelta(days=2)))
            r = fit_and_predict(matches, fx, t, s, b)
            p_close, _ = closing(conn, fx["event_id"].to_list(), config.settings.edge)
            res = {e["event_id"]: (e["home_goals"], e["away_goals"]) for e in fx.iter_rows(named=True)}
            for p in r.predictions.iter_rows(named=True):
                hg, ag = res[p["event_id"]]
                rows.append(
                    p
                    | {
                        "pool": pool,
                        "day": str(day),
                        "healthy": r.healthy,
                        "p_close": p_close.get((p["event_id"], p["market"], p["line"], p["selection"])),
                        "won": outcome(p["market"], p["selection"], p["line"], hg, ag),
                    }
                )
            print(
                f"  {pool} {day}: {fx.height} matches, r_hat={r.max_rhat:.3f}, divergences={r.divergences}", flush=True
            )

    df = pl.DataFrame(rows)
    with_close = df.filter(pl.col("p_close").is_not_null())
    inside = (with_close["p_close"] >= with_close["prob_lo"]) & (with_close["p_close"] <= with_close["prob_hi"])
    print(
        f"\n{df.height} selection-predictions over {df['day'].n_unique()} slates; "
        f"mean band width {(df['prob_hi'] - df['prob_lo']).mean():.3f}"
    )
    print(
        f"Pinnacle close inside the {b.interval:.0%} band: {inside.mean():.1%} of {with_close.height} "
        f"(1X2 {inside.filter(with_close['market'] == '1X2').mean():.1%}, "
        f"OU {inside.filter(with_close['market'] == 'OU').mean():.1%})"
    )

    bins = []
    edges = np.array([0, 0.2, 0.35, 0.5, 0.65, 1.0])
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        g = df.filter((pl.col("prob") >= lo) & (pl.col("prob") < hi))
        if g.height < 20:
            continue
        k, n = float(g["won"].sum()), g.height
        w_lo, w_hi = M.wilson(k, n)
        band = (g["prob_lo"].mean(), g["prob_hi"].mean())
        bins.append(
            {
                "bin": f"{lo:.2f}-{hi:.2f}",
                "n": n,
                "mean_p": round(g["prob"].mean(), 3),
                "realised": round(k / n, 3),
                "wilson_95": f"[{w_lo:.3f}, {w_hi:.3f}]",
                "mean_band": f"[{band[0]:.3f}, {band[1]:.3f}]",
                "realised_in_band": band[0] <= k / n <= band[1],
                "mean_p_in_wilson": w_lo <= g["prob"].mean() <= w_hi,
            }
        )
    out = pl.DataFrame(bins)
    with pl.Config(tbl_width_chars=200, tbl_cols=12):
        print(out)
    return out
