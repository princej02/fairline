"""Value board: model probability vs bookmaker price, with a sharp reference alongside.

For each (match, market) at its decision time:
1. Build one Snapshot per bookmaker: its latest complete market with every price at the same
   timestamp. Partial or mixed-timestamp markets are skipped and counted, never devigged.
2. Reference fair probabilities come from the first entry in `reference_books` with a valid
   snapshot, using `devig_method`. A conservative version (lowest fair p across methods)
   comes from the same snapshot. Inverted books (sum 1/O <= 1) are flagged and skipped.
3. Every price at a bettable book gets these numbers. Exchange prices are first converted to
   effective odds after commission.
   - ev_model = p_model * price - 1: the edge the model claims
   - ev_vs_ref = p_ref * price - 1: what the sharp market says about that price
   - ev_conservative = p_conservative * price - 1, using the posterior quantile (when bands exist)
   - Kelly from p_model; stakes are then capped per bet, match and slate (kelly.size)
4. The board keeps the best price per selection with ev_model >= min_ev and raw price <=
   max_odds. By default it keeps one selection per match (the highest EV), since markets on
   the same match are correlated.

Every number is decision support. A positive EV means the model and the price disagree,
which is often a model miss, not a guaranteed edge. That's why the reference and conservative
columns are shown next to it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime

import polars as pl

from fairline.core.config import EdgeSettings
from fairline.edge.devig import IncompleteMarket, Quote, Snapshot, conservative, devig, exchange_effective_odds
from fairline.edge.kelly import binary_outcome, full_kelly, size


@dataclass
class ModelProb:
    event_id: int
    market: str
    line: float | None
    selection: str
    p: float
    p_conservative: float | None = None
    prediction_id: int | None = None


@dataclass
class EventInfo:
    event_id: int
    starts_at: datetime
    decision_ts: datetime
    slate: str
    label: str = ""


@dataclass
class Candidate:
    event_id: int
    label: str
    starts_at: datetime
    decision_ts: datetime
    market: str
    line: float | None
    selection: str
    bookmaker: str
    odds_ts: datetime
    price: float  # as quoted
    effective_price: float  # after exchange commission
    p_model: float
    p_conservative: float | None
    p_ref: float | None
    p_ref_conservative: float | None
    ref_book: str | None
    book_overround: float
    ev_model: float
    ev_vs_ref: float | None
    ev_vs_ref_conservative: float | None
    ev_conservative: float | None
    kelly_full: float
    prediction_id: int | None
    slate: str
    flags: list[str] = field(default_factory=list)
    on_board: bool = False
    stake_pct: float = 0.0
    capped_by: tuple[str, ...] = ()


@dataclass
class Board:
    candidates: list[Candidate]
    skipped_snapshots: dict[str, int]

    def frame(self, board_only: bool = True) -> pl.DataFrame:
        rows = [asdict(c) for c in self.candidates if c.on_board or not board_only]
        for r in rows:
            r["flags"] = ",".join(r["flags"])
            r["capped_by"] = ",".join(r["capped_by"])
        return pl.DataFrame(rows) if rows else pl.DataFrame()


def _snapshots(quotes: list[Quote], market: str) -> tuple[dict[str, Snapshot], dict[str, int]]:
    """Latest complete same-timestamp snapshot per bookmaker."""
    skipped: dict[str, int] = defaultdict(int)
    by_book_ts: dict[tuple[str, datetime], list[Quote]] = defaultdict(list)
    for q in quotes:
        by_book_ts[(q.bookmaker, q.ts)].append(q)
    best: dict[str, Snapshot] = {}
    for (book, _ts), qs in sorted(by_book_ts.items(), key=lambda kv: kv[0][1]):
        try:
            best[book] = Snapshot.validate(market, qs)  # later ts overwrites earlier
        except IncompleteMarket:
            skipped["incomplete"] += 1
    return best, skipped


def build_board(
    events: list[EventInfo],
    probs: list[ModelProb],
    quotes: dict[tuple[int, str, float | None], list[Quote]],
    s: EdgeSettings,
    exchanges: set[str],
    open_exposure: float = 0.0,
) -> Board:
    """quotes: (event_id, market, line) -> every quote with ts <= that event's decision time."""
    cands, skipped = evaluate(events, probs, quotes, s, exchanges)
    for pick in select(cands, s, prob_of=lambda c: c.p_model):
        pick.candidate.on_board = True
    _stake(cands, s, open_exposure)
    return Board(cands, skipped)


def evaluate(
    events: list[EventInfo],
    probs: list[ModelProb],
    quotes: dict[tuple[int, str, float | None], list[Quote]],
    s: EdgeSettings,
    exchanges: set[str],
) -> tuple[list[Candidate], dict[str, int]]:
    """Every price at a bettable book with model, reference and flags attached. No selection."""
    ev_info = {e.event_id: e for e in events}
    model = {(p.event_id, p.market, p.line, p.selection): p for p in probs}
    cands: list[Candidate] = []
    skipped: dict[str, int] = defaultdict(int)

    for (event_id, market, line), qs in quotes.items():
        info = ev_info.get(event_id)
        if info is None:
            continue
        if any(q.ts > info.decision_ts for q in qs):
            raise ValueError(f"event {event_id}: quote after its decision time (leakage)")
        snaps, sk = _snapshots(qs, market)
        for k, v in sk.items():
            skipped[k] += v

        ref_book, ref_p, ref_cons, ref_flags = None, None, None, []
        for book in s.reference_books:
            snap = snaps.get(book)
            if snap is None:
                continue
            fp = devig(snap, s.devig_method)
            if fp.status != "ok":
                ref_flags.append(f"ref_{book}_{fp.status}")
                skipped[f"ref_{fp.status}"] += 1
                continue
            ref_book, ref_p, ref_cons = book, fp.probs, conservative(snap)
            break
        if ref_book is None:
            ref_flags.append("no_reference")

        for book in s.bettable_books:
            snap = snaps.get(book)
            if snap is None:
                continue
            for sel, price in snap.prices.items():
                mp = model.get((event_id, market, line, sel))
                if mp is None:
                    continue
                eff = exchange_effective_odds(price, s.betfair_commission) if book in exchanges else price
                flags = list(ref_flags)
                p_ref = ref_p[sel] if ref_p else None
                p_rc = ref_cons[sel] if ref_cons else None
                if p_ref is not None and abs(mp.p - p_ref) > s.disagreement_warn:
                    flags.append("model_disagrees_with_market")
                if price > s.max_odds:
                    flags.append("above_max_odds")
                if snap.overround <= 1:
                    flags.append("book_no_margin")
                ev_cons = None if mp.p_conservative is None else mp.p_conservative * eff - 1
                if ev_cons is not None and ev_cons < 0 <= mp.p * eff - 1:
                    flags.append("edge_not_robust")
                cands.append(
                    Candidate(
                        event_id=event_id,
                        label=info.label,
                        starts_at=info.starts_at,
                        decision_ts=info.decision_ts,
                        market=market,
                        line=line,
                        selection=sel,
                        bookmaker=book,
                        odds_ts=snap.ts,
                        price=price,
                        effective_price=eff,
                        p_model=mp.p,
                        p_conservative=mp.p_conservative,
                        p_ref=p_ref,
                        p_ref_conservative=p_rc,
                        ref_book=ref_book,
                        book_overround=snap.overround,
                        ev_model=mp.p * eff - 1,
                        ev_vs_ref=None if p_ref is None else p_ref * eff - 1,
                        ev_vs_ref_conservative=None if p_rc is None else p_rc * eff - 1,
                        ev_conservative=ev_cons,
                        kelly_full=full_kelly(binary_outcome(mp.p), eff),
                        prediction_id=mp.prediction_id,
                        slate=info.slate,
                        flags=flags,
                    )  # fmt: skip
                )

    return cands, dict(skipped)


@dataclass
class Pick:
    candidate: Candidate
    p: float  # probability the strategy bets on
    ev: float  # p * effective_price - 1


def select(
    cands: list[Candidate],
    s: EdgeSettings,
    prob_of: Callable[[Candidate], float | None],
    keep: Callable[[Candidate], bool] | None = None,
    min_ev: float | None = None,
) -> list[Pick]:
    """Board selection rules for any probability source: EV >= min_ev, raw price <= max_odds,
    book has a margin, best effective price per selection, then (by default) one pick per match."""
    min_ev = s.min_ev if min_ev is None else min_ev
    best: dict[tuple, Pick] = {}
    for c in cands:
        p = prob_of(c)
        if p is None or c.price > s.max_odds or "book_no_margin" in c.flags or (keep and not keep(c)):
            continue
        ev = p * c.effective_price - 1
        if ev < min_ev:
            continue
        k = (c.event_id, c.market, c.line, c.selection)  # line shopping
        if k not in best or c.effective_price > best[k].candidate.effective_price:
            best[k] = Pick(c, p, ev)
    picks = list(best.values())
    if s.one_per_match:
        top: dict[int, Pick] = {}
        for pk in picks:
            if pk.candidate.event_id not in top or pk.ev > top[pk.candidate.event_id].ev:
                top[pk.candidate.event_id] = pk
        picks = list(top.values())
    return picks


def _stake(cands: list[Candidate], s: EdgeSettings, open_exposure: float) -> None:
    board = [c for c in cands if c.on_board]
    keys = [f"{c.event_id}:{c.market}:{c.line}:{c.selection}:{c.bookmaker}" for c in board]
    stakes = size(
        [(k, c.event_id, c.slate, c.kelly_full) for k, c in zip(keys, board, strict=True)],
        s.kelly_fraction, s.max_stake_pct, s.max_match_exposure_pct, s.max_slate_exposure_pct, open_exposure,
    )  # fmt: skip
    for c, st in zip(board, stakes, strict=True):
        c.stake_pct, c.capped_by = st.stake, st.capped_by
