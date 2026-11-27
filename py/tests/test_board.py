from datetime import UTC, datetime, timedelta

import pytest

from fairline.core.config import get_config
from fairline.edge.board import EventInfo, ModelProb, build_board
from fairline.edge.devig import Quote

S = get_config().settings.edge
KO = datetime(2025, 3, 8, 15, tzinfo=UTC)
D = KO - timedelta(hours=2)
T0 = KO - timedelta(hours=26)
EX = {"betfair_ex_uk"}


def ev(i=1):
    return EventInfo(i, KO, D, slate="2025-03-08", label=f"match {i}")


def q1x2(book, h, d, a, ts=T0):
    return [Quote("home", h, ts, book), Quote("draw", d, ts, book), Quote("away", a, ts, book)]


def probs(i=1, h=0.50, d=0.27, a=0.23):
    return [
        ModelProb(i, "1X2", None, "home", h),
        ModelProb(i, "1X2", None, "draw", d),
        ModelProb(i, "1X2", None, "away", a),
    ]


def test_line_shopping_and_reference():
    quotes = {(1, "1X2", None): q1x2("pinnacle", 2.20, 3.50, 3.60) + q1x2("bet365", 2.15, 3.40, 3.50)
              + q1x2("williamhill", 2.25, 3.30, 3.40)}  # fmt: skip
    b = build_board([ev()], probs(), quotes, S, EX)
    board = [c for c in b.candidates if c.on_board]
    assert len(board) == 1 and board[0].selection == "home" and board[0].bookmaker == "williamhill"
    assert board[0].ref_book == "pinnacle" and board[0].p_ref is not None
    assert not any(c.bookmaker == "pinnacle" for c in b.candidates)  # reference only, not bettable


def test_exchange_commission_reduces_ev():
    quotes = {(1, "1X2", None): q1x2("pinnacle", 2.2, 3.5, 3.6) + q1x2("betfair_ex_uk", 2.30, 3.60, 3.70)}
    c = next(c for c in build_board([ev()], probs(), quotes, S, EX).candidates if c.selection == "home")
    assert c.effective_price == pytest.approx(1 + 1.30 * (1 - S.betfair_commission))
    assert c.ev_model == pytest.approx(0.50 * c.effective_price - 1)


def test_incomplete_and_mixed_ts_snapshots_are_skipped():
    partial = [Quote("home", 2.3, T0, "bet365"), Quote("away", 3.4, T0, "bet365")]
    mixed = [Quote("home", 2.3, T0, "skybet"), Quote("draw", 3.4, T0, "skybet"),
             Quote("away", 3.4, T0 + timedelta(minutes=1), "skybet")]  # fmt: skip
    b = build_board([ev()], probs(), {(1, "1X2", None): q1x2("pinnacle", 2.2, 3.5, 3.6) + partial + mixed}, S, EX)
    assert not any(c.bookmaker in ("bet365", "skybet") for c in b.candidates)
    assert b.skipped_snapshots["incomplete"] >= 3


def test_inverted_reference_falls_back_to_next_book():
    quotes = {(1, "1X2", None): q1x2("pinnacle", 2.6, 4.0, 4.5)            # sum 1/O < 1: inverted
              + q1x2("betfair_ex_uk", 2.2, 3.5, 3.6) + q1x2("bet365", 2.15, 3.4, 3.5)}  # fmt: skip
    c = next(c for c in build_board([ev()], probs(), quotes, S, EX).candidates if c.bookmaker == "bet365")
    assert c.ref_book == "betfair_ex_uk" and "ref_pinnacle_no_margin" in c.flags


def test_quote_after_decision_time_is_rejected():
    late = q1x2("bet365", 2.15, 3.4, 3.5, ts=D + timedelta(minutes=1))
    with pytest.raises(ValueError, match="leakage"):
        build_board([ev()], probs(), {(1, "1X2", None): late}, S, EX)


def test_one_per_match_and_max_odds():
    quotes = {(1, "1X2", None): q1x2("pinnacle", 1.68, 3.80, 5.40) + q1x2("bet365", 1.70, 3.50, 7.0)}
    b = build_board([ev()], probs(h=0.62, d=0.13, a=0.25), quotes, S, EX)
    board = [c for c in b.candidates if c.on_board]
    assert len(board) == 1 and board[0].price <= S.max_odds  # away @ 7.0 has more EV but is above max_odds
    assert "above_max_odds" in next(c for c in b.candidates if c.selection == "away").flags


def test_disagreement_and_not_robust_flags():
    quotes = {(1, "1X2", None): q1x2("pinnacle", 2.2, 3.5, 3.6) + q1x2("bet365", 2.15, 3.4, 3.5)}
    p = probs(h=0.62, d=0.22, a=0.16)
    p[0].p_conservative = 0.44
    c = next(c for c in build_board([ev()], p, quotes, S, EX).candidates if c.selection == "home")
    assert "model_disagrees_with_market" in c.flags and "edge_not_robust" in c.flags
