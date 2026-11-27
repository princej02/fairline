"""Polite, cached downloads of football-data.co.uk season CSVs.

Rules:
- Completed seasons are downloaded once and then served from the local cache forever.
- The current season is re-checked at most every `refresh_current_hours`, with a conditional
  GET (ETag / Last-Modified), so an unchanged file costs one 304 response.
- At most one request every `request_delay_seconds`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from fairline.core.config import season_start_year

USER_AGENT = "fairline/0.1 (personal research; low volume)"
_last_request_at = 0.0


def season_path_code(label: str) -> str:
    """'2024-25' -> '2425', the folder name football-data.co.uk uses."""
    y = season_start_year(label)
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


@dataclass
class Download:
    content: bytes
    from_cache: bool
    changed: bool  # False if the server said 304 / cache was fresh
    url: str
    fetched_at: datetime


class FootballDataCoUkSource:
    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        current_season: str,
        refresh_current_hours: float = 12,
        request_delay_seconds: float = 2.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.current_season = current_season
        self.refresh_after = timedelta(hours=refresh_current_hours)
        self.delay = request_delay_seconds
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)

    def url(self, division: str, season: str) -> str:
        return f"{self.base_url}/{season_path_code(season)}/{division}.csv"

    def _paths(self, division: str, season: str) -> tuple[Path, Path]:
        d = self.cache_dir / season
        return d / f"{division}.csv", d / f"{division}.meta.json"

    def fetch(self, division: str, season: str, force: bool = False) -> Download:
        csv_path, meta_path = self._paths(division, season)
        url = self.url(division, season)
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        now = datetime.now(UTC)

        if csv_path.exists() and not force:
            fetched_at = datetime.fromisoformat(meta.get("fetched_at", now.isoformat()))
            is_current = season_start_year(season) >= season_start_year(self.current_season)
            if not is_current or now - fetched_at < self.refresh_after:
                return Download(csv_path.read_bytes(), True, False, url, fetched_at)

        headers = {}
        if csv_path.exists() and not force:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        self._throttle()
        resp = self.client.get(url, headers=headers)
        if resp.status_code == 304:
            meta["fetched_at"] = now.isoformat()
            meta_path.write_text(json.dumps(meta, indent=2))
            return Download(csv_path.read_bytes(), True, False, url, now)
        resp.raise_for_status()

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        changed = not csv_path.exists() or csv_path.read_bytes() != resp.content
        csv_path.write_bytes(resp.content)
        meta_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "fetched_at": now.isoformat(),
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "bytes": len(resp.content),
                },
                indent=2,
            )
        )
        return Download(resp.content, False, changed, url, now)

    def _throttle(self) -> None:
        global _last_request_at
        wait = self.delay - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
