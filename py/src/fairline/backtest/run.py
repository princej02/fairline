"""CLI entry: run the backtest, print tables, optionally store a backtest_run row."""

from __future__ import annotations

import json
import time

import polars as pl
import psycopg

from fairline.backtest.engine import STRATEGIES, run
from fairline.core.config import Config
from fairline.models.football.walkforward import config_hash


def run_backtest(
    conn: psycopg.Connection,
    config: Config,
    pools: list[str],
    first: str,
    last: str,
    offset_hours: float = 2.0,
    persist: bool = False,
    n_boot: int = 2000,
):
    t0 = time.perf_counter()
    res = run(conn, config, pools, first, last, offset_hours, n_boot)
    secs = time.perf_counter() - t0
    with pl.Config(tbl_rows=40, tbl_cols=30, tbl_width_chars=260, fmt_str_lengths=40):
        print(f"\nBetting ({', '.join(pools)}, {first}..{last}, decided at kickoff - {offset_hours:g}h)")
        print(res.summary)
        print("\nProbability accuracy (same matches for every source; minus_close < 0 means better than the close)")
        print(res.prob_summary)
        per = (
            res.bets.group_by("strategy", "season")
            .agg(pl.len().alias("bets"), pl.col("flat_pnl").mean().round(3).alias("roi"))
            .pivot(on="season", index="strategy", values="roi")
            .sort("strategy")
        )
        per = per.select("strategy", *sorted(c for c in per.columns if c != "strategy"))
        print("\nFlat ROI by season")
        print(per)
        print("\nModel calibration (bins with n >= 30)")
        for market, bins in res.calibration.items():
            line = " ".join(f"{b.mean_p:.2f}->{b.observed:.2f}{'' if b.consistent else '*'}" for b in bins if b.n >= 30)
            print(f"  {market}: {line}   (* = outside 95% Wilson interval)")
        print(
            "\nBlend weight on model, fitted on prior seasons:",
            {f"{p}/{x}": v for (p, x), v in res.blend_weights.items()},
        )
    print(f"\n{secs:.0f}s")
    if persist:
        cfg = {
            "pools": pools,
            "first": first,
            "last": last,
            "offset_hours": offset_hours,
            "strategies": [s.name for s in STRATEGIES],
            "edge": config.settings.edge.model_dump(),
            "model_config_hash": config_hash(config.settings.models.dixon_coles),
        }
        metrics = {
            "betting": res.summary.to_dicts(),
            "probability": res.prob_summary.to_dicts(),
            "blend_weights": {f"{p}/{x}": v for (p, x), v in res.blend_weights.items()},
        }
        bid = conn.execute(
            "INSERT INTO backtest_run (config, metrics, finished_at) VALUES (%s, %s, now()) RETURNING id",
            (json.dumps(cfg, default=str), json.dumps(metrics, default=str)),
        ).fetchone()[0]
        conn.commit()
        print(f"stored backtest_run id={bid}")
    return res
