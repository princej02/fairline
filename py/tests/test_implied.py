"""Market-implied O/U 2.5 from Pinnacle's 1X2 + main totals line (pure)."""

from datetime import UTC, datetime, timedelta

import pytest

from fairline.core.config import get_config
from fairline.edge.board import EventInfo, ModelProb, evaluate
from fairline.edge.devig import Quote
from fairline.ingest.the_odds_api.derive import derive_totals
from fairline.ingest.the_odds_api.parse import ParsedOdds
from fairline.models.football.implied import NoFit, implied_totals
from fairline.models.football.markets import asian_total, one_x_two, over_under
from fairline.models.football.scores import score_matrix


@pytest.mark.parametrize(
    "lh, la, rho, line",
    [
        (1.6, 1.1, -0.05, 2.75),
        (2.6, 0.7, -0.06, 3.25),
        (0.8, 2.1, -0.05, 3.0),
        (1.2, 1.25, 0.01, 2.25),
        (1.9, 1.5, -0.04, 3.5),
    ],
)
def test_round_trip_recovers_the_matrix(lh, la, rho, line):
    m = score_matrix(lh, la, rho)
    r = implied_totals(one_x_two(m), line, asian_total(m, line, "over").effective_prob, rho)
    assert r.lambda_home == pytest.approx(lh, abs=1e-5) and r.lambda_away == pytest.approx(la, abs=1e-5)
    assert r.p_over == pytest.approx(over_under(m, 2.5)["over"], abs=1e-6) and abs(r.draw_fit_error) < 1e-6


def test_tau_matters_only_when_the_main_line_reaches_down_to_two_goals():
    # tau moves mass among 0-0, 1-0, 0-1, 1-1 (all totals <= 2) and preserves it, so P(3+ goals),
    # and with it O/U 2.5, is unchanged for main lines >= 2.75. On a 2.25 line settlement does see
    # totals of 2 vs fewer, and Dixon-Coles differs from a plain-Poisson fit by ~0.2-0.3pp.
    for lh, la, line, differs in [(1.6, 1.1, 2.75, False), (1.5, 1.3, 3.0, False), (1.3, 1.1, 2.25, True)]:
        m = score_matrix(lh, la, -0.06)
        args = (one_x_two(m), line, asian_total(m, line, "over").effective_prob)
        gap = implied_totals(*args, -0.06).p_over - implied_totals(*args, 0.0).p_over
        assert (abs(gap) > 1e-3) is differs, (line, gap)


def test_unreachable_inputs_raise():
    with pytest.raises(NoFit):
        implied_totals({"home": 0.4, "draw": 0.3, "away": 0.3}, 3.0, 0.999999, -0.05)


T = datetime(2025, 1, 18, 14, 0, tzinfo=UTC)


def _parsed(totals, with_quoted_25=False):
    p = ParsedOdds()
    p.sources["x"] = {"h2h": {"home": (1.8, T), "draw": (3.9, T), "away": (4.6, T)}, "totals": totals}
    if with_quoted_25:
        p.rows.append({"external_id": "x", "bookmaker": "pinnacle", "market": "OU", "line": 2.5, "selection": "over",
                       "price": 1.9, "ts": T})  # fmt: skip
    return p


def test_derive_from_main_line_and_record_provenance():
    p = _parsed([(3.0, 2.08, 1.82, T + timedelta(minutes=1))])
    d = derive_totals(p, {"x": 7}, "pinnacle", "pinnacle_derived", 2.5, -0.05, "power")
    assert {r["selection"] for r in d.rows} == {"over", "under"} and all(r["event_id"] == 7 for r in d.rows)
    assert all(r["bookmaker"] == "pinnacle_derived" and r["line"] == 2.5 for r in d.rows)
    assert sum(1 / r["price"] for r in d.rows) == pytest.approx(1, abs=2e-3)  # margin-free
    (rec,) = d.records
    assert rec["source_line"] == 3.0 and rec["ts"] == T + timedelta(minutes=1)  # the later input
    assert p.books_seen["x"] == {"pinnacle_derived"}  # so the close poll copies it


def test_quoted_line_wins_and_missing_inputs_skip():
    assert derive_totals(_parsed([(3.0, 2.08, 1.82, T)], with_quoted_25=True), {"x": 7}, "pinnacle", "pinnacle_derived",
                         2.5, -0.05, "power").skipped == {"quoted": 1}  # fmt: skip
    assert derive_totals(_parsed([]), {"x": 7}, "pinnacle", "pinnacle_derived", 2.5, -0.05, "power").skipped == {
        "no_inputs": 1
    }


def _ou(book, over, under, ts):
    return [Quote("over", over, ts, book), Quote("under", under, ts, book)]


def test_board_uses_the_fresher_of_quoted_and_derived():
    s = get_config().settings.edge
    assert s.derived_references == {"pinnacle": "pinnacle_derived"}
    ev = EventInfo(1, T + timedelta(hours=3), T + timedelta(hours=1), slate="d", label="A v B")
    stale_quoted = _ou("pinnacle", 1.95, 1.95, T - timedelta(hours=20))
    fresh_derived = _ou("pinnacle_derived", 2.10, 1.909, T)
    soft = _ou("williamhill", 2.2, 1.7, T)
    probs = [ModelProb(1, "OU", 2.5, sel, p, None, None) for sel, p in (("over", 0.5), ("under", 0.5))]
    cands, _ = evaluate([ev], probs, {(1, "OU", 2.5): stale_quoted + fresh_derived + soft}, s, set())
    (c,) = [c for c in cands if c.selection == "over"]
    assert c.ref_book == "pinnacle_derived" and "ref_derived" in c.flags
    assert c.p_ref == pytest.approx((1 / 2.10) / (1 / 2.10 + 1 / 1.909))  # normalised, not devigged

    tie = _ou("pinnacle", 2.0, 1.9, T)
    cands, _ = evaluate([ev], probs, {(1, "OU", 2.5): tie + fresh_derived + soft}, s, set())
    assert {c.ref_book for c in cands} == {"pinnacle"}  # same timestamp: quoted wins
