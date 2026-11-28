"""Provider team names -> our participants, provider fixtures -> our events. Shared by every
live provider (The Odds API, football-data.org).

Team names (football-data.co.uk spellings are canonical, e.g. "Man United", "Nott'm Forest"):
1. An existing the_odds_api alias wins (human-reviewed or earlier auto-match).
2. Otherwise compare normalised names against teams seen in this country's competitions over
   the last three seasons (so promoted/relegated sides are candidates):
   - exact match after normalising (accents, punctuation, FC/AFC, Man->Manchester, ...), or
   - one name's tokens are a subset of the other's ("Tottenham" vs "Tottenham Hotspur"),
   and only when exactly one candidate qualifies and no other provider name claims it.
   Auto-matches with a different spelling are stored with reviewed = false so
   `fairline odds aliases` can show them; identical spellings are stored as reviewed.
3. Anything else goes to participant_alias_review and that fixture's odds are skipped. A wrong
   match would silently attach prices to the wrong team, which is worse than a missing match.

Events: a league season has exactly one Home v Away fixture, so a league fixture matches the
event with the same season, home and away regardless of kickoff (rearranged games move by
weeks). Cups fall back to the same pairing within 3 days.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg

from fairline.core.config import Competition, season_label_for

_EXPAND = {
    "man": "manchester",
    "utd": "united",
    "nottm": "nottingham",
    "weds": "wednesday",
    "sheff": "sheffield",
    "brom": "bromwich",
    "ath": "athletic",
    "st": "saint",
    "ste": "sainte",
}
_DROP = {"fc", "afc", "cf", "sc", "ac", "club", "the", "and", "de", "of"}


def normalise(name: str) -> tuple[str, ...]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "")
    toks = re.sub(r"[^a-z0-9]+", " ", s).split()
    return tuple(_EXPAND.get(t, t) for t in toks if t not in _DROP)


@dataclass
class TeamResolution:
    ids: dict[str, int] = field(default_factory=dict)
    auto_matched: dict[str, str] = field(default_factory=dict)  # provider name -> our name
    unresolved: dict[str, list[str]] = field(default_factory=dict)  # provider name -> candidates


def match_names(
    names: list[str], candidates: dict[int, str], variants: dict[str, list[str]] | None = None
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Pure matching step. Returns (matched name -> participant id, unresolved name -> candidate names).
    `variants` gives extra spellings to try for a name (e.g. a provider's short name), in order;
    the first spelling with exactly one qualifying candidate wins."""
    cand = {pid: set(normalise(n)) for pid, n in candidates.items()}
    exact = {pid: normalise(n) for pid, n in candidates.items()}
    matched: dict[str, int] = {}
    unresolved: dict[str, list[str]] = {}
    for name in names:
        seen: set[int] = set()
        for spelling in [*(variants or {}).get(name, []), name]:
            toks = normalise(spelling)
            hits = [pid for pid, t in exact.items() if t == toks]
            if not hits:
                st = set(toks)
                hits = [pid for pid, c in cand.items() if c and st and (c <= st or st <= c)]
            if len(hits) == 1:
                matched[name] = hits[0]
                break
            seen.update(hits)
        else:
            unresolved[name] = sorted(candidates[p] for p in seen)
    # two provider names must never collapse onto one team
    claimed: dict[int, list[str]] = {}
    for n, pid in matched.items():
        claimed.setdefault(pid, []).append(n)
    for pid, ns in claimed.items():
        if len(ns) > 1:
            for n in ns:
                matched.pop(n)
                unresolved[n] = [candidates[pid]]
    return matched, unresolved


def _candidates(conn: psycopg.Connection, comp: Competition, sport_id: int, now: datetime) -> dict[int, str]:
    seasons = [season_label_for((now - timedelta(days=365 * k)).date()) for k in range(3)]
    rows = conn.execute(
        """
        SELECT DISTINCT p.id, p.name
        FROM event e JOIN season s ON s.id = e.season_id JOIN competition c ON c.id = s.competition_id
        JOIN participant p ON p.id IN (e.home_id, e.away_id)
        WHERE p.sport_id = %s AND s.label = ANY(%s)
          AND (c.code = %s OR (%s::text IS NOT NULL AND c.country = %s))
        """,
        (sport_id, seasons, comp.code, comp.country, comp.country),
    ).fetchall()
    return dict(rows)


def resolve_teams(
    conn: psycopg.Connection,
    provider: str,
    comp: Competition,
    sport_id: int,
    names: list[str],
    now: datetime,
    variants: dict[str, list[str]] | None = None,
) -> TeamResolution:
    out = TeamResolution()
    names = sorted(set(names))
    known = dict(
        conn.execute(
            "SELECT alias, participant_id FROM participant_alias WHERE provider = %s AND sport_id = %s AND alias = ANY(%s)",
            (provider, sport_id, names),
        ).fetchall()
    )
    out.ids.update(known)
    todo = [n for n in names if n not in known]
    if not todo:
        return out
    cands = _candidates(conn, comp, sport_id, now)
    # teams already aliased to another provider name are not up for grabs
    taken = {
        pid
        for (pid,) in conn.execute(
            "SELECT participant_id FROM participant_alias WHERE provider = %s AND sport_id = %s",
            (provider, sport_id),
        ).fetchall()
    }
    matched, unresolved = match_names(todo, {p: n for p, n in cands.items() if p not in taken}, variants)
    for name, pid in matched.items():
        identical = cands[pid] in (name, *(variants or {}).get(name, []))  # same spelling: nothing to check
        conn.execute(
            "INSERT INTO participant_alias (provider, sport_id, alias, participant_id, reviewed) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (provider, sport_id, name, pid, identical),
        )
        conn.execute(
            "DELETE FROM participant_alias_review WHERE provider = %s AND sport_id = %s AND alias = %s",
            (provider, sport_id, name),
        )
        out.ids[name] = pid
        if not identical:
            out.auto_matched[name] = cands[pid]
    for name, c in unresolved.items():
        conn.execute(
            """
            INSERT INTO participant_alias_review (provider, sport_id, alias, competition, candidates)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (provider, sport_id, alias) DO UPDATE
              SET last_seen = now(), times_seen = participant_alias_review.times_seen + 1,
                  candidates = EXCLUDED.candidates
            """,
            (provider, sport_id, name, comp.code, c),
        )
    out.unresolved = unresolved
    return out


def _season_id(conn, comp_id: int, label: str, day) -> int:
    return conn.execute(
        """
        INSERT INTO season (competition_id, label, starts_on, ends_on) VALUES (%s, %s, %s, %s)
        ON CONFLICT (competition_id, label) DO UPDATE
          SET starts_on = LEAST(season.starts_on, EXCLUDED.starts_on),
              ends_on = GREATEST(season.ends_on, EXCLUDED.ends_on)
        RETURNING id
        """,
        (comp_id, label, day, day),
    ).fetchone()[0]


def resolve_event(
    conn: psycopg.Connection,
    provider: str,
    comp: Competition,
    comp_id: int,
    external_id: str,
    home_id: int,
    away_id: int,
    commence: datetime,
    authoritative: bool = False,
) -> tuple[int, str]:
    """Returns (event_id, how): how is 'known' | 'linked' | 'created'. Keeps a scheduled event's
    kickoff in step with the provider. `authoritative` providers (football-data.org) may also move
    a postponed/void fixture's kickoff; the caller sets status separately."""
    row = conn.execute(
        "SELECT event_id FROM event_external_id WHERE provider = %s AND external_id = %s", (provider, external_id)
    ).fetchone()
    how = "known"
    if row is None:
        season_id = _season_id(conn, comp_id, season_label_for(commence.date()), commence.date())
        if comp.kind == "league":
            row = conn.execute(
                "SELECT id FROM event WHERE season_id = %s AND home_id = %s AND away_id = %s",
                (season_id, home_id, away_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM event WHERE season_id = %s AND home_id = %s AND away_id = %s "
                "AND abs(extract(epoch FROM starts_at - %s)) < 3 * 86400",
                (season_id, home_id, away_id, commence),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO event (season_id, starts_at, kickoff_time_known, home_id, away_id, status) "
                "VALUES (%s, %s, true, %s, %s, 'scheduled') RETURNING id",
                (season_id, commence, home_id, away_id),
            ).fetchone()
            how = "created"
        else:
            how = "linked"
        conn.execute(
            "INSERT INTO event_external_id (provider, external_id, event_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (provider, external_id, row[0]),
        )
    movable = ("scheduled", "postponed", "void") if authoritative else ("scheduled",)
    conn.execute(
        "UPDATE event SET starts_at = %s, kickoff_time_known = true, updated_at = now() "
        "WHERE id = %s AND status = ANY(%s) AND starts_at IS DISTINCT FROM %s",
        (commence, row[0], list(movable), commence),
    )
    return row[0], how
