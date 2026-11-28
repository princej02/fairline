"""football-data.org v4 client: token in a header, polite throttle, every body cached.

Free tier: 10 requests/minute, no monthly cap. We stay under it with one request every
`request_delay_seconds` and honour a 429's `X-RequestCounter-Reset` (seconds) once.
The token travels in the X-Auth-Token header, never in the URL, so URLs are safe to log.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

USER_AGENT = "fairline/0.1 (personal research; low volume)"
_last_request_at = 0.0


class FdoError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"football-data.org {status}: {message}")
        self.status = status


@dataclass
class Response:
    data: dict
    fetched_at: datetime
    cache_path: Path
    available_minute: int | None


def token() -> str:
    from fairline.core.db import database_url  # loads .env

    database_url()
    t = os.environ.get("FOOTBALL_DATA_ORG_TOKEN", "").strip()
    if not t:
        raise FdoError(0, "FOOTBALL_DATA_ORG_TOKEN is not set in .env (free key: football-data.org/client/register)")
    return t


class FdoClient:
    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        delay: float = 6.5,
        token_: str | None = None,
        client: httpx.Client | None = None,
        now=lambda: datetime.now(UTC),
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.delay = delay
        self._token = token_
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
        self.now = now
        self.sleep = sleep

    def season(self, code: str, start_year: int) -> Response:
        return self._get(code, f"season{start_year}", {"season": start_year})

    def window(self, code: str, date_from: date, date_to: date) -> Response:
        return self._get(code, "window", {"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()})

    def _throttle(self) -> None:
        global _last_request_at
        wait = self.delay - (time.monotonic() - _last_request_at)
        if wait > 0:
            self.sleep(wait)
        _last_request_at = time.monotonic()

    def _get(self, code: str, kind: str, params: dict) -> Response:
        if self._token is None:
            self._token = token()
        url = f"{self.base_url}/competitions/{code}/matches"
        for attempt in range(2):
            self._throttle()
            try:
                resp = self.client.get(url, params=params, headers={"X-Auth-Token": self._token})
            except httpx.HTTPError as e:
                raise FdoError(0, f"{type(e).__name__} fetching {code}") from None
            if resp.status_code == 429 and attempt == 0:
                self.sleep(float(resp.headers.get("X-RequestCounter-Reset", 60)) + 1)
                continue
            break
        if resp.status_code != 200:
            try:
                msg = resp.json().get("message", resp.reason_phrase)
            except ValueError:
                msg = resp.reason_phrase
            raise FdoError(resp.status_code, msg)
        fetched = self.now()
        d = self.cache_dir / code / fetched.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{fetched:%H%M%S%f}_{kind}.json"
        path.write_bytes(resp.content)
        (path.with_suffix(".meta.json")).write_text(json.dumps({"fetched_at": fetched.isoformat(), "params": params}))
        avail = resp.headers.get("X-Requests-Available-Minute")
        return Response(resp.json(), fetched, path, int(avail) if avail and avail.isdigit() else None)
