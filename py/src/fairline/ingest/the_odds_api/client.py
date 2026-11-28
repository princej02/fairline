"""The Odds API v4 client: cached, credit-aware, and careful with the key.

- `/sports/{sport}/events` is free; it is cached on disk for `events_refresh_hours`.
- `/sports/{sport}/odds` costs `markets x regions` credits (with `bookmakers=`, each 10 books bill
  as one region). Every response body is written to the cache before anything parses it.
- The key travels as a query parameter, so httpx's own request logging is silenced and errors
  are rebuilt without the URL: the key never reaches logs, exceptions or the cache metadata.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO line prints the URL incl. apiKey

USER_AGENT = "fairline/0.1 (personal research; low volume)"


class OddsApiError(RuntimeError):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"The Odds API {status} {code or ''}: {message}".strip())
        self.status, self.code = status, code


@dataclass(frozen=True)
class Usage:
    remaining: int | None
    used: int | None
    last: int | None  # credits this call cost

    @classmethod
    def from_headers(cls, h: httpx.Headers) -> Usage:
        def num(k):
            v = h.get(k)
            try:
                return int(float(v)) if v is not None else None
            except ValueError:
                return None

        return cls(num("x-requests-remaining"), num("x-requests-used"), num("x-requests-last"))


@dataclass
class Response:
    data: list[dict]
    usage: Usage
    fetched_at: datetime
    cache_path: Path
    from_cache: bool = False


def api_key() -> str:
    from fairline.core.db import database_url  # loads .env

    database_url()
    key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    if not key:
        raise OddsApiError(0, "MISSING_KEY", "THE_ODDS_API_KEY is not set in .env")
    return key


class OddsApiClient:
    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        key: str | None = None,
        events_refresh: timedelta = timedelta(hours=6),
        client: httpx.Client | None = None,
        now=lambda: datetime.now(UTC),
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self._key = key
        self.events_refresh = events_refresh
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
        self.now = now

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = api_key()
        return self._key

    # ------------------------------------------------------------------ endpoints
    def events(self, sport: str, force: bool = False) -> Response:
        """Upcoming fixtures for one sport key. Free. Served from cache while fresh."""
        if not force and (hit := self._fresh_events(sport)) is not None:
            return hit
        return self._get(sport, "events", f"/sports/{sport}/events", {"dateFormat": "iso"})

    def odds(self, sport: str, bookmakers: list[str], markets: str) -> Response:
        """Pre-match prices only: commenceTimeFrom=now keeps in-play prices out."""
        params = {
            "bookmakers": ",".join(bookmakers),
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "commenceTimeFrom": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return self._get(sport, "odds", f"/sports/{sport}/odds", params)

    # ------------------------------------------------------------------ internals
    def _get(self, sport: str, kind: str, path: str, params: dict) -> Response:
        for attempt in range(3):
            try:
                resp = self.client.get(self.base_url + path, params={**params, "apiKey": self.key})
            except httpx.HTTPError as e:  # str(e) can include the URL; keep only the class name
                raise OddsApiError(0, type(e).__name__, f"request to {path} failed") from None
            if resp.status_code == 429 and attempt < 2:  # frequency limit, not credits
                time.sleep(1.0 + attempt)
                continue
            break
        usage = Usage.from_headers(resp.headers)
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise OddsApiError(resp.status_code, body.get("error_code"), body.get("message", resp.reason_phrase))
        fetched = self.now()
        path_out = self._write(sport, kind, fetched, resp.content, params, usage)
        return Response(resp.json(), usage, fetched, path_out)

    def _dir(self, sport: str, when: datetime) -> Path:
        return self.cache_dir / sport / when.strftime("%Y-%m-%d")

    def _write(self, sport, kind, when: datetime, body: bytes, params: dict, usage: Usage) -> Path:
        d = self._dir(sport, when)
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{when:%H%M%S%f}_{kind}"
        (d / f"{stem}.json").write_bytes(body)
        meta = {"fetched_at": when.isoformat(), "params": params, "usage": usage.__dict__}  # no apiKey
        (d / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2))
        return d / f"{stem}.json"

    def _fresh_events(self, sport: str) -> Response | None:
        root = self.cache_dir / sport
        if not root.exists():
            return None
        metas = sorted(root.glob("*/*_events.meta.json"))
        if not metas:
            return None
        meta = json.loads(metas[-1].read_text())
        fetched = datetime.fromisoformat(meta["fetched_at"])
        if self.now() - fetched >= self.events_refresh:
            return None
        body = metas[-1].with_name(metas[-1].name.replace(".meta.json", ".json"))
        return Response(json.loads(body.read_bytes()), Usage(None, None, 0), fetched, body, from_cache=True)
