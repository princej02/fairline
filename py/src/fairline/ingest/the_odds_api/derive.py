"""Derived O/U reference prices from a poll (see fairline.models.football.implied).

For each event where the source book (Pinnacle) priced 1X2 and a totals line but not the target
line itself, fit the Dixon-Coles matrix to its devigged 1X2 + main line and store the implied
target-line prices under the derived pseudo-book (pinnacle_derived), with an odds_derivation
row saying how. A quoted target line from the same poll always wins: nothing is derived then.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from fairline.core.config import Config
from fairline.edge.devig import IncompleteMarket, Quote, Snapshot, devig
from fairline.ingest.the_odds_api.parse import ParsedOdds
from fairline.models.football.implied import NoFit, implied_totals

METHOD = "dixon_coles_1x2_mainline"


@dataclass
class Derived:
    rows: list[dict] = field(default_factory=list)  # odds_snapshot rows, same shape as parse rows + event_id
    records: list[dict] = field(default_factory=list)  # odds_derivation rows
    skipped: dict[str, int] = field(default_factory=dict)


def latest_rho(conn: psycopg.Connection, comp_code: str, default: float) -> float:
    row = conn.execute(
        """
        SELECT pr.rho FROM prediction_rate pr JOIN event e ON e.id = pr.event_id
        JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        WHERE c.code = %s ORDER BY pr.as_of DESC LIMIT 1
        """,
        (comp_code,),
    ).fetchone()
    return float(row[0]) if row else default


def _main_line(totals: list[tuple], exclude: float) -> tuple | None:
    """The source book's main line: the most balanced pair it quoted, other than `exclude`."""
    cands = [t for t in totals if t[0] != exclude]
    return min(cands, key=lambda t: abs(1 / t[1] - 1 / t[2]), default=None)


def derive_totals(
    parsed: ParsedOdds, ext_to_event: dict[str, int], source: str, derived: str, target: float, rho: float, method: str
) -> Derived:
    out = Derived()

    def skip(reason: str) -> None:
        out.skipped[reason] = out.skipped.get(reason, 0) + 1

    quoted = {
        r["external_id"]
        for r in parsed.rows
        if r["bookmaker"] == source and r["market"] == "OU" and r["line"] == target
    }
    for ext, src in parsed.sources.items():
        if ext not in ext_to_event:
            continue
        if ext in quoted:
            skip("quoted")
            continue
        main = _main_line(src["totals"], target)
        if len(src["h2h"]) != 3 or main is None:
            skip("no_inputs")
            continue
        line, over, under, t_tot = main
        try:
            p1 = devig(Snapshot.validate("1X2", [Quote(s, p, t, source) for s, (p, t) in src["h2h"].items()]), method)
            pt = devig(
                Snapshot.validate("OU", [Quote("over", over, t_tot, source), Quote("under", under, t_tot, source)]),
                method,
            )
        except IncompleteMarket:
            skip("incomplete")
            continue
        if p1.status != "ok" or pt.status != "ok":
            skip(f"devig_{p1.status if p1.status != 'ok' else pt.status}")
            continue
        try:
            fit = implied_totals(p1.probs, line, pt.probs["over"], rho, target)
        except NoFit:
            skip("no_fit")
            continue
        ts = max(t_tot, *(t for _, t in src["h2h"].values()))
        eid = ext_to_event[ext]
        for sel, p in (("over", fit.p_over), ("under", fit.p_under)):
            out.rows.append({"external_id": ext, "event_id": eid, "bookmaker": derived, "market": "OU", "line": target,
                             "selection": sel, "price": round(1 / p, 3), "ts": ts})  # fmt: skip
        out.records.append({"event_id": eid, "ts": ts, "source_line": line, "source_over": over, "source_under": under,
                            "lambda_home": fit.lambda_home, "lambda_away": fit.lambda_away, "rho": fit.rho,
                            "p_over": fit.p_over, "draw_fit_error": fit.draw_fit_error})  # fmt: skip
        parsed.books_seen.setdefault(ext, set()).add(derived)
    return out


def store_records(
    conn: psycopg.Connection, sport_id: int, derived: str, source: str, target: float, records: list[dict]
) -> int:
    n = 0
    for r in records:
        n += conn.execute(
            """
            INSERT INTO odds_derivation (event_id, bookmaker_id, is_closing, ts, market_id, source_book_id, source_line,
              source_over, source_under, lambda_home, lambda_away, rho, p_over, draw_fit_error, method)
            SELECT %(event_id)s, d.id, false, %(ts)s, m.id, s.id, %(source_line)s, %(source_over)s, %(source_under)s,
                   %(lambda_home)s, %(lambda_away)s, %(rho)s, %(p_over)s, %(draw_fit_error)s, %(method)s
            FROM bookmaker d, bookmaker s, market m
            WHERE d.code = %(derived)s AND s.code = %(source)s AND m.sport_id = %(sport_id)s AND m.code = 'OU' AND m.line = %(target)s
            ON CONFLICT DO NOTHING
            """,
            r | {"derived": derived, "source": source, "sport_id": sport_id, "target": target, "method": METHOD},
        ).rowcount
    return n


def config_for(config: Config) -> tuple[str, str] | None:
    """(source, derived) pair, e.g. (pinnacle, pinnacle_derived), or None when not configured."""
    for code, b in config.bookmakers.items():
        if b.kind == "derived" and b.derived_from:
            return b.derived_from, code
    return None


__all__ = ["Derived", "config_for", "derive_totals", "latest_rho", "store_records"]
