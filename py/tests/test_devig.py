from datetime import UTC, datetime, timedelta

import pytest

from fairline.edge.devig import (
    IncompleteMarket,
    Quote,
    Snapshot,
    conservative,
    devig,
    devig_all,
    exchange_effective_odds,
)

T = datetime(2025, 3, 8, 13, tzinfo=UTC)


def snap(market, prices, book="pinnacle", ts=T):
    return Snapshot.validate(market, [Quote(s, p, ts, book) for s, p in prices.items()])


ONE_X_TWO = snap("1X2", {"home": 1.62, "draw": 4.10, "away": 5.60})
LOPSIDED = snap("OU", {"over": 1.25, "under": 4.20})


@pytest.mark.parametrize("method", ["multiplicative", "additive", "power", "shin"])
def test_every_method_sums_to_one(method):
    for s in (ONE_X_TWO, LOPSIDED):
        r = devig(s, method)
        assert r.status == "ok"
        assert sum(r.probs.values()) == pytest.approx(1, abs=1e-12)


def test_power_and_shin_strip_more_from_longshot_than_multiplicative():
    m, p, s = (devig(LOPSIDED, k).probs["under"] for k in ("multiplicative", "power", "shin"))
    assert p < m and s < m


def test_shin_equals_additive_in_two_way_but_not_three_way():
    for prices in ({"over": 1.25, "under": 4.20}, {"over": 1.91, "under": 1.95}, {"over": 1.5, "under": 2.6}):
        s = snap("OU", prices)
        assert devig(s, "shin").probs == pytest.approx(devig(s, "additive").probs, abs=1e-9)
    sh, ad = devig(ONE_X_TWO, "shin").probs, devig(ONE_X_TWO, "additive").probs
    assert max(abs(sh[k] - ad[k]) for k in sh) > 1e-4


def test_inverted_book_is_flagged_not_solved():
    inv = snap("OU", {"over": 2.10, "under": 2.05}, book="betfair_ex_uk")  # sum(1/O) < 1
    for r in devig_all(inv).values():
        assert r.status == "no_margin" and r.probs is None
    assert conservative(inv) is None
    exact = snap("OU", {"over": 2.0, "under": 2.0})  # sum(1/O) == 1
    assert devig(exact, "shin").status == "no_margin"


def test_additive_invalid_when_longshot_goes_negative():
    s = snap("1X2", {"home": 1.10, "draw": 8.0, "away": 101.0})  # margin/3 > 1/101
    assert devig(s, "additive").status == "invalid"
    assert devig(s, "power").status == "ok"


def test_conservative_is_highest_fair_odds_across_methods():
    c = conservative(LOPSIDED)
    for method in ("multiplicative", "additive", "power", "shin"):
        p = devig(LOPSIDED, method).probs
        assert c["under"] <= p["under"] + 1e-15 and c["over"] <= p["over"] + 1e-15


def test_rejects_partial_mixed_book_and_mixed_timestamp_markets():
    with pytest.raises(IncompleteMarket, match="missing"):
        Snapshot.validate("1X2", [Quote("home", 1.6, T, "pinnacle"), Quote("away", 5.6, T, "pinnacle")])
    with pytest.raises(IncompleteMarket, match="bookmakers"):
        Snapshot.validate("OU", [Quote("over", 1.9, T, "pinnacle"), Quote("under", 1.9, T, "bet365")])
    with pytest.raises(IncompleteMarket, match="timestamps"):
        Snapshot.validate(
            "OU", [Quote("over", 1.9, T, "pinnacle"), Quote("under", 1.9, T + timedelta(minutes=5), "pinnacle")]
        )


def test_exchange_commission_on_net_winnings():
    assert exchange_effective_odds(3.0, 0.05) == pytest.approx(2.9)
    assert exchange_effective_odds(1.5, 0.02) == pytest.approx(1.49)
