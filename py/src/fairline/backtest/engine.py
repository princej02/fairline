"""Walk-forward betting backtest over several seasons and pools.

Leakage rules:
- Model probabilities come from walk-forward fits with as_of <= decision time (edge.data).
- Prices and the sharp reference come from quotes with ts <= decision time.
- Blend weights for season S are fitted only on seasons before S.
- Closing prices are used only to *score* bets (CLV), never to choose them.

Strategies (all use the same evaluated prices, selection rules and staking caps):
- model:              Dixon-Coles probability
- model_beats_ref:    model, but only when the price also beats the devigged sharp price
- model_no_disagree:  model, minus picks flagged model_disagrees_with_market
- reference:          pure soft-book hunting: devigged sharp price as truth, model ignored
- blend:              w * model + (1 - w) * sharp, w fitted on prior seasons (log loss)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import polars as pl
import psycopg

from fairline.backtest import metrics as M
from fairline.backtest.data import closing, outcome, season_events
from fairline.core.config import Config, season_range
from fairline.edge.board import Candidate, evaluate, select
from fairline.edge.data import load_inputs
from fairline.edge.kelly import binary_outcome, full_kelly, size
from fairline.models.football.walkforward import MODEL_NAME, config_hash

BLEND_GRID = np.round(np.linspace(0, 1, 21), 3)
PROB_SCHEMA = {
    "pool": pl.String,
    "season": pl.String,
    "competition": pl.String,
    "slate": pl.String,
    "event_id": pl.Int64,
    "market": pl.String,
    "line": pl.Float64,
    "selection": pl.String,
    "p_model": pl.Float64,
    "p_ref": pl.Float64,
    "p_close": pl.Float64,
    "won": pl.Boolean,
}


@dataclass
class Strategy:
    name: str
    prob_of: Callable[[Candidate, float], float | None]  # (candidate, blend weight) -> p
    keep: Callable[[Candidate], bool] | None = None


STRATEGIES = [
    Strategy("model", lambda c, w: c.p_model),
    Strategy("model_beats_ref", lambda c, w: c.p_model, keep=lambda c: c.ev_vs_ref is not None and c.ev_vs_ref >= 0),
    Strategy("model_no_disagree", lambda c, w: c.p_model, keep=lambda c: "model_disagrees_with_market" not in c.flags),
    Strategy("reference", lambda c, w: c.p_ref, keep=lambda c: c.bookmaker != c.ref_book),
    Strategy(
        "blend",
        lambda c, w: None if c.p_ref is None or np.isnan(w) else w * c.p_model + (1 - w) * c.p_ref,
    ),
]


@dataclass
class SeasonData:
    pool: str
    season: str
    candidates: list[Candidate]
    probs: pl.DataFrame  # one row per (event, market, selection): p_model, p_ref, p_close, won
    meta: dict
    p_close: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    bets: pl.DataFrame  # every pick of every strategy
    probs: pl.DataFrame  # all matches, for probability metrics
    blend_weights: dict[tuple[str, str], float]
    summary: pl.DataFrame
    prob_summary: pl.DataFrame
    calibration: dict[str, list[M.CalibrationBin]]
    notes: list[str] = field(default_factory=list)


def load_season(conn: psycopg.Connection, config: Config, pool: str, season: str, offset_hours: float) -> SeasonData:
    st = config.settings
    s = st.edge
    comps = st.models.dixon_coles.pools[pool]
    events, meta = season_events(conn, comps, season, offset_hours)
    if not events:
        return SeasonData(pool, season, [], pl.DataFrame(), meta, {})
    books = sorted(set(s.reference_books) | set(s.bettable_books))
    probs, quotes = load_inputs(conn, events, books, MODEL_NAME, config_hash(st.models.dixon_coles))
    exchanges = {b.code for b in config.bookmakers.values() if b.kind == "exchange"}
    cands, _ = evaluate(events, probs, quotes, s, exchanges)
    p_close, _close_price = closing(conn, [e.event_id for e in events], s)

    rows = {}
    for c in cands:
        k = (c.event_id, c.market, c.line, c.selection)
        if k in rows:
            continue
        m = meta[c.event_id]
        rows[k] = {
            "pool": pool,
            "season": season,
            "competition": m["competition"],
            "slate": c.slate,
            "event_id": c.event_id,
            "market": c.market,
            "line": c.line,
            "selection": c.selection,
            "p_model": c.p_model,
            "p_ref": c.p_ref,
            "p_close": p_close.get(k),
            "won": outcome(c.market, c.selection, c.line, m["home_goals"], m["away_goals"]),
        }
    return SeasonData(pool, season, cands, pl.DataFrame(list(rows.values()), schema=PROB_SCHEMA), meta, p_close)


def fit_blend_weight(prior: pl.DataFrame) -> float:
    """w in [0, 1] minimising log loss of w*model + (1-w)*sharp on prior seasons (won rows only)."""
    d = prior.filter(pl.col("won") & pl.col("p_ref").is_not_null())
    if d.height < 200:
        return float("nan")
    pm, pr = d["p_model"].to_numpy(), d["p_ref"].to_numpy()
    losses = [M.log_loss(w * pm + (1 - w) * pr).mean() for w in BLEND_GRID]
    return float(BLEND_GRID[int(np.argmin(losses))])


def run(
    conn: psycopg.Connection,
    config: Config,
    pools: list[str],
    first: str,
    last: str,
    offset_hours: float = 2.0,
    n_boot: int = 2000,
) -> BacktestResult:
    s = config.settings.edge
    fd = config.settings.ingest["football_data_co_uk"]
    seasons = season_range(first, last)
    warmup = season_range(fd["backfill_from"], first)[:-1]  # earlier seasons: blend fitting only
    data: dict[tuple[str, str], SeasonData] = {}
    for pool in pools:
        for season in warmup + seasons:
            data[(pool, season)] = load_season(conn, config, pool, season, offset_hours)

    weights: dict[tuple[str, str], float] = {}
    for pool in pools:
        for season in seasons:
            prior = [data[(pool, x)].probs for x in warmup + seasons if x < season and data[(pool, x)].probs.height]
            weights[(pool, season)] = fit_blend_weight(pl.concat(prior)) if prior else float("nan")

    bet_rows = []
    for strat in STRATEGIES:
        picks = []
        for pool in pools:
            for season in seasons:
                sd = data[(pool, season)]
                w = weights[(pool, season)]
                for pk in select(sd.candidates, s, prob_of=lambda c, w=w, st=strat: st.prob_of(c, w), keep=strat.keep):
                    picks.append((pool, season, sd, pk))
        # One bankroll across all pools: same-day bets share the slate cap.
        stakes = size(
            [
                (
                    str(i),
                    pk.candidate.event_id,
                    pk.candidate.slate,
                    full_kelly(binary_outcome(pk.p), pk.candidate.effective_price),
                )
                for i, (_, _, _, pk) in enumerate(picks)
            ],
            s.kelly_fraction,
            s.max_stake_pct,
            s.max_match_exposure_pct,
            s.max_slate_exposure_pct,
        )
        for (pool, season, sd, pk), st in zip(picks, stakes, strict=True):
            c = pk.candidate
            m = sd.meta[c.event_id]
            won = outcome(c.market, c.selection, c.line, m["home_goals"], m["away_goals"])
            p_close = sd.p_close.get((c.event_id, c.market, c.line, c.selection))
            bet_rows.append(
                {
                    "strategy": strat.name,
                    "pool": pool,
                    "season": season,
                    "slate": c.slate,
                    "event_id": c.event_id,
                    "label": c.label,
                    "market": c.market,
                    "selection": c.selection,
                    "bookmaker": c.bookmaker,
                    "price": c.price,
                    "effective_price": c.effective_price,
                    "p": pk.p,
                    "ev": pk.ev,
                    "p_model": c.p_model,
                    "p_ref": c.p_ref,
                    "p_close": p_close,
                    "clv": None if p_close is None else c.effective_price * p_close - 1,
                    "won": won,
                    "flat_pnl": (c.effective_price - 1) if won else -1.0,
                    "stake": st.stake,
                    "kelly_pnl": st.stake * ((c.effective_price - 1) if won else -1.0),
                    "capped_by": ",".join(st.capped_by),
                }
            )

    bets = pl.DataFrame(bet_rows)
    probs = pl.concat([data[(p, x)].probs for p in pools for x in seasons if data[(p, x)].probs.height])
    probs = probs.join(
        pl.DataFrame([{"pool": p, "season": x, "w": w} for (p, x), w in weights.items()]), on=["pool", "season"]
    ).with_columns((pl.col("w") * pl.col("p_model") + (1 - pl.col("w")) * pl.col("p_ref")).alias("p_blend"))
    summary = summarise(bets, n_boot)
    prob_summary, calib = probability_metrics(probs, n_boot)
    return BacktestResult(bets, probs, weights, summary, prob_summary, calib)


def bankroll_path(b: pl.DataFrame) -> np.ndarray:
    """Stakes are fractions of the bankroll at the start of each slate; slates settle in order."""
    per_slate = b.group_by("slate").agg(pl.col("kelly_pnl").sum()).sort("slate")
    return np.cumprod(1 + per_slate["kelly_pnl"].to_numpy())


def summarise(bets: pl.DataFrame, n_boot: int) -> pl.DataFrame:
    rows = []
    for strat in [x.name for x in STRATEGIES]:
        b = bets.filter(pl.col("strategy") == strat)
        if b.is_empty():
            rows.append({"strategy": strat, "bets": 0})
            continue
        cl = b["slate"].to_numpy()
        roi, roi_lo, roi_hi = M.cluster_bootstrap(b["flat_pnl"].to_numpy(), cl, n_boot=n_boot)
        clv_b = b.filter(pl.col("clv").is_not_null())
        clv, clv_lo, clv_hi = M.cluster_bootstrap(clv_b["clv"].to_numpy(), clv_b["slate"].to_numpy(), n_boot=n_boot)
        stake = b["stake"].to_numpy()
        kroi, kroi_lo, kroi_hi = M.cluster_bootstrap(
            b["flat_pnl"].to_numpy(), cl, n_boot=n_boot, weights=np.where(stake > 0, stake, 0)
        )
        path = bankroll_path(b)
        rows.append(
            {
                "strategy": strat,
                "bets": b.height,
                "hit": round(b["won"].mean(), 3),
                "avg_odds": round(b["effective_price"].mean(), 2),
                "mean_ev": round(b["ev"].mean(), 3),
                "flat_roi": round(roi, 4),
                "flat_roi_ci": f"[{roi_lo:+.3f}, {roi_hi:+.3f}]",
                "clv": round(clv, 4),
                "clv_ci": f"[{clv_lo:+.3f}, {clv_hi:+.3f}]",
                "beat_close": round((clv_b["clv"] > 0).mean(), 3),
                "kelly_roi": round(kroi, 4),
                "kelly_roi_ci": f"[{kroi_lo:+.3f}, {kroi_hi:+.3f}]",
                "bankroll_x": round(float(path[-1]), 3),
                "max_dd": round(M.max_drawdown(np.concatenate([[1.0], path])), 3),
                "slate_cap_rate": round(b["capped_by"].str.contains("slate").mean(), 3),
                "avg_stake": round(float(stake.mean()), 4),
            }
        )
    return pl.DataFrame(rows)


def probability_metrics(probs: pl.DataFrame, n_boot: int):
    """Log loss and Brier per market for model, blend, sharp-at-decision and sharp close, on
    the same matches (those with every source available). Paired CI for model minus close."""
    out, calib = [], {}
    sources = ["p_model", "p_blend", "p_ref", "p_close"]
    for market in ("1X2", "OU"):
        d = probs.filter((pl.col("market") == market) & pl.all_horizontal(pl.col(sources).is_not_null()))
        k = 3 if market == "1X2" else 2
        complete = d.group_by("event_id").len().filter(pl.col("len") == k)["event_id"]
        d = d.filter(pl.col("event_id").is_in(complete)).sort("event_id", "selection")
        won = d.filter("won")
        clusters = won["slate"].to_numpy()
        ll = {src: M.log_loss(won[src].to_numpy()) for src in sources}
        order = sorted(d["selection"].unique().to_list())
        for src in sources:
            mat = d.pivot(on="selection", index="event_id", values=src).sort("event_id").select(order).to_numpy()
            oc = d.filter("won").sort("event_id")["selection"].to_list()
            idx = np.array([order.index(x) for x in oc])
            br = M.brier(mat / mat.sum(1, keepdims=True), idx)
            est, lo, hi = M.cluster_bootstrap(ll[src], clusters, n_boot=n_boot)
            row = {
                "market": market,
                "source": src.removeprefix("p_"),
                "matches": len(idx),
                "log_loss": round(est, 4),
                "log_loss_ci": f"[{lo:.4f}, {hi:.4f}]",
                "brier": round(float(br.mean()), 4),
            }
            if src != "p_close":
                diff, dlo, dhi = M.cluster_bootstrap(ll[src] - ll["p_close"], clusters, n_boot=n_boot)
                row["minus_close"] = f"{diff:+.4f} [{dlo:+.4f}, {dhi:+.4f}]"
            out.append(row)
        calib[market] = M.calibration(d["p_model"].to_numpy(), d["won"].to_numpy().astype(float))
    return pl.DataFrame(out), calib
