"""The Odds API v4 JSON -> fixtures and odds rows. Pure; no DB.

Response shape (GET /sports/{sport}/odds):
  [{id, sport_key, commence_time, home_team, away_team,
    bookmakers: [{key, title, last_update,
                  markets: [{key: "h2h"|"totals", last_update, outcomes: [{name, price, point?}]}]}]}]

Timestamps: a row's ts is the market's `last_update` (when that price was set), which is never
later than our poll, so it is a true "observable from" time (ts_estimated = false). Anything
updated at or after kickoff is dropped: in-play prices must never look like pre-match ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fairline.ingest.the_odds_api.planner import Fixture


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


@dataclass
class ParsedEvent:
    external_id: str
    home: str
    away: str
    commence: datetime


@dataclass
class ParsedOdds:
    events: list[ParsedEvent] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)  # external_id, bookmaker, market, line, selection, price, ts
    books_seen: dict[str, set[str]] = field(default_factory=dict)  # external_id -> our book codes
    # inputs for derived prices, from the `source_book` only: external_id -> {"h2h": {sel: (price, ts)},
    # "totals": [(line, over, under, ts)]} with every totals line it quoted, 2.5 or not
    sources: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_events(data: list[dict]) -> list[ParsedEvent]:
    return [ParsedEvent(e["id"], e["home_team"], e["away_team"], ts(e["commence_time"])) for e in data]


def fixtures(data: list[dict]) -> list[Fixture]:
    return [Fixture(e.external_id, e.commence) for e in parse_events(data)]


def parse_odds(
    data: list[dict],
    book_map: dict[str, str],
    ou_lines: set[float],
    polled_at: datetime,
    source_book: str | None = None,
) -> ParsedOdds:
    """book_map: provider bookmaker key -> our bookmaker code (books we do not track are ignored).
    source_book: our code of the book whose 1X2 + main totals line feed derived prices."""
    out = ParsedOdds()
    for e in data:
        ev = ParsedEvent(e["id"], e["home_team"], e["away_team"], ts(e["commence_time"]))
        out.events.append(ev)
        sel_1x2 = {ev.home: "home", ev.away: "away", "Draw": "draw"}
        for b in e.get("bookmakers", []):
            code = book_map.get(b["key"])
            if code is None:
                continue
            for m in b.get("markets", []):
                t = ts(m.get("last_update") or b.get("last_update") or polled_at.isoformat())
                if t >= ev.commence:  # check before clamping, or a skewed in-play stamp slips through
                    out.warnings.append(f"{ev.external_id} {code} {m['key']}: updated after kickoff, dropped")
                    continue
                t = min(t, polled_at)  # never claim a price was observable after we saw it
                rows = []
                if code == source_book:
                    _capture_source(out, ev, m, t)
                for o in m.get("outcomes", []):
                    price = float(o["price"])
                    if price <= 1:
                        continue
                    if m["key"] == "h2h":
                        sel = sel_1x2.get(o["name"])
                        if sel is None:
                            out.warnings.append(f"{ev.external_id} {code}: h2h outcome {o['name']!r} not a team")
                            continue
                        rows.append(("1X2", None, sel, price))
                    elif m["key"] == "totals":
                        line = float(o.get("point", "nan"))
                        if line in ou_lines and o["name"] in ("Over", "Under"):
                            rows.append(("OU", line, o["name"].lower(), price))
                for market, line, sel, price in rows:
                    out.rows.append(
                        {
                            "external_id": ev.external_id,
                            "bookmaker": code,
                            "market": market,
                            "line": line,
                            "selection": sel,
                            "price": price,
                            "ts": t,
                        }
                    )
                if rows:
                    out.books_seen.setdefault(ev.external_id, set()).add(code)
    return out


def _capture_source(out: ParsedOdds, ev: ParsedEvent, m: dict, t: datetime) -> None:
    src = out.sources.setdefault(ev.external_id, {"h2h": {}, "totals": []})
    names = {ev.home: "home", ev.away: "away", "Draw": "draw"}
    if m["key"] == "h2h":
        for o in m.get("outcomes", []):
            if o["name"] in names and float(o["price"]) > 1:
                src["h2h"][names[o["name"]]] = (float(o["price"]), t)
    elif m["key"] == "totals":
        by: dict[float, dict] = {}
        for o in m.get("outcomes", []):
            if o.get("point") is not None and o["name"] in ("Over", "Under") and float(o["price"]) > 1:
                by.setdefault(float(o["point"]), {})[o["name"]] = float(o["price"])
        src["totals"] += [(ln, p["Over"], p["Under"], t) for ln, p in by.items() if len(p) == 2]
