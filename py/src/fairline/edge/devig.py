"""Remove bookmaker margin from one complete market snapshot to get fair probabilities.

Guards:
- A market is devigged only if it is complete (every outcome priced), and all prices come
  from the same bookmaker at the same timestamp (`Snapshot.validate`).
- If sum(1/O) <= 1 there is no margin to remove (inverted book / arbitrage, common on thin
  exchange markets). Shin and Power are undefined there, so the market is *flagged*
  (status "no_margin") instead of being passed to a root-finder.

Methods (q_i = 1/O_i, Q = sum q_i):
- multiplicative: q_i / Q. Removes margin proportionally, so it leaves longshots too likely.
- additive:       q_i - (Q - 1) / n. Can go negative for big longshots (flagged).
- power:          q_i ** k with k chosen so the sum is 1. Strips more from longshots.
- shin:           Shin (1992) insider-trading model. In a 2-way market it is identical to
                  additive; it differs only with 3+ outcomes (1X2).

`conservative()` returns, per selection, the lowest fair probability (highest fair odds)
across valid methods: a worst-case check for backing that selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy.optimize import brentq

METHODS = ("multiplicative", "additive", "power", "shin")
REQUIRED = {"1X2": ("home", "draw", "away"), "OU": ("over", "under"), "BTTS": ("yes", "no")}


class IncompleteMarket(ValueError):
    pass


@dataclass(frozen=True)
class Quote:
    selection: str
    price: float
    ts: datetime
    bookmaker: str


@dataclass(frozen=True)
class Snapshot:
    """All prices for one market from one bookmaker at one instant."""

    market: str
    bookmaker: str
    ts: datetime
    prices: dict[str, float]

    @classmethod
    def validate(cls, market: str, quotes: list[Quote]) -> Snapshot:
        required = REQUIRED.get(market)
        if required is None:
            raise IncompleteMarket(f"no outcome set defined for market {market!r}")
        books = {q.bookmaker for q in quotes}
        stamps = {q.ts for q in quotes}
        if len(books) != 1:
            raise IncompleteMarket(f"prices from several bookmakers {sorted(books)}")
        if len(stamps) != 1:
            raise IncompleteMarket(f"prices from {len(stamps)} different timestamps")
        prices = {q.selection: float(q.price) for q in quotes}
        missing = set(required) - prices.keys()
        extra = prices.keys() - set(required)
        if missing or extra or len(quotes) != len(required):
            raise IncompleteMarket(f"{market} needs exactly {required}; missing {missing}, extra {extra}")
        if min(prices.values()) <= 1.0:
            raise IncompleteMarket("price <= 1.0")
        return cls(market, books.pop(), stamps.pop(), {s: prices[s] for s in required})

    @property
    def overround(self) -> float:
        return sum(1 / p for p in self.prices.values())


@dataclass(frozen=True)
class FairPrices:
    method: str
    probs: dict[str, float] | None  # None when status != "ok"
    overround: float
    status: str  # ok | no_margin | invalid
    reason: str = ""

    def fair_odds(self, selection: str) -> float | None:
        return None if self.probs is None else 1 / self.probs[selection]


def _multiplicative(q: np.ndarray) -> np.ndarray:
    return q / q.sum()


def _additive(q: np.ndarray) -> np.ndarray:
    return q - (q.sum() - 1) / q.size


def _power(q: np.ndarray) -> np.ndarray:
    k = brentq(lambda k: (q**k).sum() - 1, 1.0, 200.0, xtol=1e-14)
    return q**k


def _shin(q: np.ndarray) -> np.ndarray:
    total = q.sum()

    def probs(z: float) -> np.ndarray:
        return (np.sqrt(z * z + 4 * (1 - z) * q * q / total) - z) / (2 * (1 - z))

    z = brentq(lambda z: probs(z).sum() - 1, 0.0, 0.999, xtol=1e-14)
    return probs(z)


_FUNCS = {"multiplicative": _multiplicative, "additive": _additive, "power": _power, "shin": _shin}


def devig(snap: Snapshot, method: str) -> FairPrices:
    if method not in _FUNCS:
        raise ValueError(f"unknown devig method {method!r}; choose from {METHODS}")
    sels = list(snap.prices)
    q = np.array([1 / snap.prices[s] for s in sels])
    over = float(q.sum())
    if over <= 1.0:
        return FairPrices(method, None, over, "no_margin", f"sum(1/O) = {over:.4f} <= 1: inverted book, flagged")
    p = _FUNCS[method](q)
    if (p <= 0).any() or not np.isfinite(p).all():
        return FairPrices(method, None, over, "invalid", f"{method} gave non-positive probabilities")
    p = p / p.sum()  # remove float residue from the root-finders
    return FairPrices(method, dict(zip(sels, p.tolist(), strict=True)), over, "ok")


def devig_all(snap: Snapshot) -> dict[str, FairPrices]:
    return {m: devig(snap, m) for m in METHODS}


def conservative(snap: Snapshot) -> dict[str, float] | None:
    """Per selection, the minimum fair probability over valid methods (= highest fair odds)."""
    valid = [r.probs for r in devig_all(snap).values() if r.status == "ok"]
    if not valid:
        return None
    return {s: min(p[s] for p in valid) for s in snap.prices}


def exchange_effective_odds(price: float, commission: float) -> float:
    """What a winning exchange back bet actually pays after commission on net winnings."""
    return 1 + (price - 1) * (1 - commission)


def reference_fair(snap: Snapshot, method: str, derived: bool) -> FairPrices:
    """Fair probabilities for a reference snapshot. A derived snapshot (e.g. pinnacle_derived) was
    computed margin-free, so it is normalised, not devigged: devigging a price set that sums to 1
    would trip the no-margin guard."""
    if not derived:
        return devig(snap, method)
    sels = list(snap.prices)
    q = np.array([1 / snap.prices[s] for s in sels])
    return FairPrices("derived", dict(zip(sels, (q / q.sum()).tolist(), strict=True)), float(q.sum()), "ok")
