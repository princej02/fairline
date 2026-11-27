"""The Odds API poller: pure parts (cost, budget, planner, parser, name matching, client hygiene)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fairline.core.config import get_config, season_label_for
from fairline.ingest.the_odds_api import budget as B
from fairline.ingest.the_odds_api.client import OddsApiClient, OddsApiError
from fairline.ingest.the_odds_api.parse import parse_odds
from fairline.ingest.the_odds_api.planner import Fixture, plan, simulate
from fairline.ingest.the_odds_api.resolve import match_names, normalise

S = get_config().settings.odds_api
T0 = datetime(2025, 1, 18, 15, 0, tzinfo=UTC)  # a Saturday 15:00 kickoff


def test_call_cost_bills_ten_books_as_one_region():
    assert B.call_cost("h2h,totals", 5) == 2
    assert B.call_cost("h2h,totals", 10) == 2
    assert B.call_cost("h2h,totals", 11) == 4
    assert B.call_cost("h2h", 0) == 1


def test_season_label_for():
    assert season_label_for(datetime(2026, 8, 1).date()) == "2026-27"
    assert season_label_for(datetime(2027, 5, 20).date()) == "2026-27"


class TestBudget:
    mid = datetime(2025, 1, 16, 12, tzinfo=UTC)  # ~half the month gone

    def test_allows_normal_spend(self):
        assert B.decide("window", 2, 100, 400, self.mid, S).allowed

    def test_hard_floor_from_api_remaining(self):
        d = B.decide("close", 2, 0, S.reserve_credits + 1, self.mid, S)
        assert not d.allowed and "reserve" in d.reason

    def test_monthly_cap_applies_even_to_close_polls(self):
        d = B.decide("close", 2, S.monthly_credit_budget - S.reserve_credits - 1, None, self.mid, S)
        assert not d.allowed and "monthly budget" in d.reason

    def test_pacing_drops_window_polls_but_keeps_close_polls(self):
        spent = int(S.monthly_credit_budget * (B.month_elapsed(self.mid) + S.pace_slack))  # right at pace
        assert not B.decide("window", 2, spent, None, self.mid, S).allowed
        assert B.decide("close", 2, spent, None, self.mid, S).allowed

    def test_warns_past_threshold(self):
        assert B.decide("close", 2, int(S.monthly_credit_budget * S.warn_at), None, self.mid, S).warn


class TestPlanner:
    fx = {"soccer_epl": [Fixture("a" * 8, T0), Fixture("b" * 8, T0 + timedelta(hours=2, minutes=30))]}

    def test_nothing_before_the_first_window(self):
        assert plan(self.fx, {}, T0 - timedelta(hours=25), S) == []

    def test_window_poll_once_then_quiet(self):
        now = T0 - timedelta(hours=23)
        (need,) = plan(self.fx, {}, now, S)
        assert need.kind == "window"
        assert plan(self.fx, {"soccer_epl": now}, now + timedelta(minutes=5), S) == []

    def test_close_poll_inside_close_window(self):
        now = T0 - timedelta(minutes=S.close_window_minutes - 1)
        (need,) = plan(self.fx, {"soccer_epl": T0 - timedelta(hours=3)}, now, S)
        assert need.kind == "close"

    def test_close_polls_sort_first(self):
        fx = self.fx | {"soccer_efl_champ": [Fixture("c" * 8, T0 + timedelta(hours=20))]}
        now = T0 - timedelta(minutes=10)
        needs = plan(fx, {"soccer_epl": T0 - timedelta(hours=3)}, now, S)
        assert [n.kind for n in needs] == ["close", "window"]

    def test_started_fixtures_are_ignored(self):
        assert plan({"x": [Fixture("z" * 8, T0)]}, {}, T0 + timedelta(minutes=1), S) == []

    def test_simulation_merges_overlapping_windows(self):
        # kickoffs 15:00 and 17:30: one T-24h poll covers both (tolerance), 15:00's T-3h at 12:00,
        # 17:30's T-3h (14:30) waits for 15:00's close poll at 14:40, then 17:30's close.
        polls = simulate(self.fx, T0 - timedelta(days=2), T0 + timedelta(hours=4), S)
        assert [(t.strftime("%d %H:%M"), k) for t, _, k in polls] == [
            ("17 15:00", "window"),
            ("18 12:00", "window"),
            ("18 14:40", "close"),
            ("18 17:10", "close"),
        ]

    def test_window_poll_deferred_into_imminent_close(self):
        now = T0 + timedelta(hours=2, minutes=30) - timedelta(hours=3)  # b's T-3h opens at 14:30
        assert plan(self.fx, {"soccer_epl": T0 - timedelta(hours=3)}, now, S) == []  # a closes at 14:40


def _event(eid, home, away, commence, books):
    return {"id": eid, "sport_key": "soccer_epl", "commence_time": commence, "home_team": home, "away_team": away,
            "bookmakers": books}  # fmt: skip


def _book(key, last_update, h2h=None, totals=None):
    markets = []
    if h2h:
        markets.append(
            {"key": "h2h", "last_update": last_update, "outcomes": [{"name": n, "price": p} for n, p in h2h]}
        )
    if totals:
        markets.append({"key": "totals", "last_update": last_update,
                        "outcomes": [{"name": n, "price": p, "point": pt} for n, p, pt in totals]})  # fmt: skip
    return {"key": key, "title": key, "last_update": last_update, "markets": markets}


def test_parse_maps_selections_lines_and_books():
    polled = datetime(2025, 1, 18, 14, 41, tzinfo=UTC)
    data = [
        _event("e1", "Fulham", "Manchester United", "2025-01-18T15:00:00Z", [
            _book("pinnacle", "2025-01-18T14:39:00Z", h2h=[("Fulham", 3.1), ("Manchester United", 2.4), ("Draw", 3.5)],
                  totals=[("Over", 1.9, 2.5), ("Under", 1.95, 2.5), ("Over", 2.6, 3.5), ("Under", 1.5, 3.5)]),
            _book("unibet", "2025-01-18T14:39:00Z", h2h=[("Fulham", 3.0), ("Manchester United", 2.5), ("Draw", 3.4)]),
            _book("williamhill", "2025-01-18T15:02:00Z", h2h=[("Fulham", 3.0), ("Manchester United", 2.5), ("Draw", 3.4)]),
        ]),
    ]  # fmt: skip
    out = parse_odds(data, {"pinnacle": "pinnacle", "williamhill": "williamhill"}, {2.5}, polled)
    sel = {(r["market"], r["line"], r["selection"]): r["price"] for r in out.rows}
    assert sel == {("1X2", None, "home"): 3.1, ("1X2", None, "away"): 2.4, ("1X2", None, "draw"): 3.5,
                   ("OU", 2.5, "over"): 1.9, ("OU", 2.5, "under"): 1.95}  # fmt: skip
    assert all(r["bookmaker"] == "pinnacle" for r in out.rows)  # unibet untracked, WH updated in-play
    assert out.books_seen == {"e1": {"pinnacle"}}
    assert any("after kickoff" in w for w in out.warnings)
    assert all(r["ts"] <= polled for r in out.rows)


@pytest.mark.parametrize(
    "provider, ours",
    [
        ("Manchester United", "Man United"),
        ("Manchester City", "Man City"),
        ("Nottingham Forest", "Nott'm Forest"),
        ("Tottenham Hotspur", "Tottenham"),
        ("Brighton and Hove Albion", "Brighton"),
        ("AFC Bournemouth", "Bournemouth"),
        ("Sheffield Wednesday", "Sheffield Weds"),
        ("West Bromwich Albion", "West Brom"),
        ("Borussia Dortmund", "Dortmund"),
        ("Saint-Etienne", "St Etienne"),
        ("Atlético Madrid", "Atletico Madrid"),
    ],
)
def test_name_matching_accepts_unambiguous(provider, ours):
    cands = dict(enumerate(["Man United", "Man City", "Nott'm Forest", "Tottenham", "Brighton", "Bournemouth",
                            "Sheffield Weds", "Sheffield United", "West Brom", "West Ham", "Dortmund",
                            "St Etienne", "Atletico Madrid", "Real Madrid", "Wolves"]))  # fmt: skip
    matched, unresolved = match_names([provider], cands)
    assert cands[matched[provider]] == ours and not unresolved


def test_name_matching_refuses_guesses():
    cands = {1: "Wolves", 2: "Paris SG", 3: "Sheffield United", 4: "Sheffield Weds", 5: "Man United"}
    matched, unresolved = match_names(["Wolverhampton Wanderers", "Paris Saint Germain", "Sheffield"], cands)
    assert matched == {}
    assert unresolved["Sheffield"] == ["Sheffield United", "Sheffield Weds"]  # ambiguous, both offered


def test_two_provider_names_never_share_a_team():
    matched, unresolved = match_names(["Tottenham", "Tottenham Hotspur"], {1: "Tottenham"})
    assert matched == {} and set(unresolved) == {"Tottenham", "Tottenham Hotspur"}


def test_normalise():
    assert normalise("Nott'm Forest") == ("nottingham", "forest")
    assert normalise("Brighton & Hove Albion FC") == ("brighton", "hove", "albion")


# ------------------------------------------------------------------ client hygiene
KEY = "secret-test-key-123"


def _client(tmp_path, handler, now=lambda: T0):
    return OddsApiClient("https://api.example.test/v4", tmp_path, key=KEY,
                         client=httpx.Client(transport=httpx.MockTransport(handler)), now=now)  # fmt: skip


def test_client_caches_without_key_and_reads_usage(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url)
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "480", "x-requests-used": "20",
                                                     "x-requests-last": "2"})  # fmt: skip

    c = _client(tmp_path, handler)
    r = c.odds("soccer_epl", ["pinnacle", "betfair_ex_uk"], "h2h,totals")
    assert (r.usage.remaining, r.usage.used, r.usage.last) == (480, 20, 2)
    q = seen[0].params
    assert (
        q["bookmakers"] == "pinnacle,betfair_ex_uk"
        and "regions" not in q
        and q["commenceTimeFrom"] == "2025-01-18T15:00:00Z"
    )
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text()


def test_events_are_served_from_cache_while_fresh(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=[{"id": "x", "commence_time": "2025-01-19T15:00:00Z",
                                          "home_team": "A", "away_team": "B"}])  # fmt: skip

    now = [T0]
    c = _client(tmp_path, handler, now=lambda: now[0])
    c.events("soccer_epl")
    now[0] = T0 + timedelta(hours=1)
    assert c.events("soccer_epl").from_cache and len(calls) == 1
    now[0] = T0 + timedelta(hours=S.events_refresh_hours + 1)
    assert not c.events("soccer_epl").from_cache and len(calls) == 2


def test_errors_never_carry_the_key(tmp_path):
    def handler(req):
        return httpx.Response(
            401, json={"message": "Usage quota has been reached", "error_code": "OUT_OF_USAGE_CREDITS"}
        )

    with pytest.raises(OddsApiError) as e:
        _client(tmp_path, handler).odds("soccer_epl", ["pinnacle"], "h2h")
    assert e.value.code == "OUT_OF_USAGE_CREDITS" and KEY not in str(e.value)

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    with pytest.raises(OddsApiError) as e:
        _client(tmp_path, boom).odds("soccer_epl", ["pinnacle"], "h2h")
    assert KEY not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_settings_reject_a_close_age_shorter_than_the_window():
    from fairline.core.config import TheOddsApiSettings

    with pytest.raises(ValueError):
        TheOddsApiSettings(**(S.model_dump() | {"close_max_age_minutes": 10}))
    json.dumps(S.model_dump())  # settings stay serialisable for run metadata
