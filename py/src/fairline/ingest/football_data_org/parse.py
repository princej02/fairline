"""football-data.org v4 /competitions/{code}/matches -> plain match records. Pure.

Status mapping (their -> ours): SCHEDULED/TIMED -> scheduled (SCHEDULED = date fixed, kickoff time
not yet), IN_PLAY/PAUSED/LIVE -> live, FINISHED/AWARDED -> finished, POSTPONED/SUSPENDED ->
postponed, CANCELLED -> void.

Scores: 1X2 and totals settle on 90 minutes, so when a cup tie goes to extra time or penalties
(score.duration != REGULAR) we take score.regularTime, never fullTime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUS = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "IN_PLAY": "live",
    "PAUSED": "live",
    "LIVE": "live",
    "FINISHED": "finished",
    "AWARDED": "finished",
    "POSTPONED": "postponed",
    "SUSPENDED": "postponed",
    "CANCELLED": "void",
}


@dataclass(frozen=True)
class Team:
    id: int
    name: str
    short: str | None


@dataclass(frozen=True)
class Match:
    external_id: str
    kickoff: datetime
    kickoff_known: bool
    status: str
    raw_status: str
    matchday: int | None
    home: Team
    away: Team
    score: tuple[int, int] | None  # 90-minute score, only when finished
    half_time: tuple[int, int] | None


def _team(t: dict) -> Team | None:
    if not t or t.get("id") is None or not t.get("name"):
        return None  # e.g. a cup tie whose participants are not decided yet
    return Team(int(t["id"]), t["name"], t.get("shortName"))


def _pair(d: dict | None) -> tuple[int, int] | None:
    if not d or d.get("home") is None or d.get("away") is None:
        return None
    return int(d["home"]), int(d["away"])


def parse_matches(data: dict) -> tuple[list[Match], list[str]]:
    out, warnings = [], []
    for m in data.get("matches", []):
        home, away = _team(m.get("homeTeam")), _team(m.get("awayTeam"))
        if home is None or away is None:
            continue
        raw = m.get("status", "")
        status = STATUS.get(raw)
        if status is None:
            warnings.append(f"{m['id']}: unknown status {raw!r}, skipped")
            continue
        score = None
        if status == "finished":
            sc = m.get("score") or {}
            score = (
                _pair(sc.get("regularTime"))
                if sc.get("duration", "REGULAR") != "REGULAR"
                else _pair(sc.get("fullTime"))
            )
            if score is None:
                warnings.append(f"{m['id']}: finished without a 90-minute score")
        out.append(
            Match(
                str(m["id"]),
                datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")),
                raw != "SCHEDULED",
                status,
                raw,
                m.get("matchday"),
                home,
                away,
                score,
                _pair((m.get("score") or {}).get("halfTime")),
            )
        )
    return out, warnings
