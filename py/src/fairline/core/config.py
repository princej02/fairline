"""Loads config/settings.toml, competitions.yaml and bookmakers.yaml.

The competition list is a strict allowlist: `Config.competition(code)` raises for anything
not listed, and every pipeline goes through it.
"""

from __future__ import annotations

import os
import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


def repo_root() -> Path:
    env = os.environ.get("FAIRLINE_ROOT")
    if env:
        return Path(env)
    # py/src/fairline/core/config.py -> repo root is 4 levels up from this file's package dir
    return Path(__file__).resolve().parents[4]


class NotAllowed(ValueError):
    """Raised when something outside the configured allowlist is requested."""


class Competition(BaseModel):
    model_config = ConfigDict(frozen=True)

    sport: str
    code: str
    name: str
    country: str | None
    kind: Literal["league", "cup", "continental", "international"]
    providers: dict[str, str | None] = Field(default_factory=dict)

    def provider_code(self, provider: str) -> str:
        code = self.providers.get(provider)
        if not code:
            raise NotAllowed(f"{self.code} has no {provider} mapping in competitions.yaml")
        return code


class Bookmaker(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    kind: Literal["book", "exchange", "aggregate"]
    is_sharp: bool
    providers: dict[str, str | None] = Field(default_factory=dict)


class EdgeSettings(BaseModel):
    kelly_fraction: float = Field(gt=0, le=1)
    max_stake_pct: float = Field(gt=0, le=1)
    min_ev: float
    max_odds: float = Field(gt=1)
    devig_method: Literal["multiplicative", "power", "shin"]
    reference_books: list[str]
    betfair_commission: float = Field(ge=0, lt=1)


class FootballDataCoUkSettings(BaseModel):
    base_url: str
    backfill_from: str
    current_season: str
    refresh_current_hours: float
    request_delay_seconds: float
    cache_dir: str


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    edge: EdgeSettings
    markets: dict[str, Any]
    ingest: dict[str, Any]

    @property
    def fdcuk(self) -> FootballDataCoUkSettings:
        return FootballDataCoUkSettings(**self.ingest["football_data_co_uk"])


_SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")


def season_start_year(label: str) -> int:
    m = _SEASON_RE.match(label)
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise ValueError(f"bad season label {label!r}; expected e.g. '2024-25'")
    return int(m.group(1))


def season_range(first: str, last: str) -> list[str]:
    a, b = season_start_year(first), season_start_year(last)
    return [f"{y}-{(y + 1) % 100:02d}" for y in range(a, b + 1)]


class Config(BaseModel):
    root: Path
    settings: Settings
    competitions: dict[str, Competition]
    bookmakers: dict[str, Bookmaker]

    def competition(self, code: str) -> Competition:
        try:
            return self.competitions[code]
        except KeyError:
            raise NotAllowed(f"competition {code!r} is not in the allowlist (config/competitions.yaml)") from None

    def competitions_for(self, provider: str, sport: str = "football") -> list[Competition]:
        return [c for c in self.competitions.values() if c.sport == sport and c.providers.get(provider)]

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def load_config(root: Path | None = None) -> Config:
    root = root or repo_root()
    cfg_dir = root / "config"
    settings = Settings(**tomllib.loads((cfg_dir / "settings.toml").read_text()))

    comps_raw = yaml.safe_load((cfg_dir / "competitions.yaml").read_text())
    competitions: dict[str, Competition] = {}
    for sport, items in comps_raw.items():
        for item in items or []:
            comp = Competition(sport=sport, **item)
            if comp.code in competitions:
                raise ValueError(f"duplicate competition code {comp.code}")
            competitions[comp.code] = comp

    books_raw = yaml.safe_load((cfg_dir / "bookmakers.yaml").read_text())
    bookmakers = {b["code"]: Bookmaker(**b) for b in books_raw["bookmakers"]}

    unknown = set(settings.edge.reference_books) - bookmakers.keys()
    if unknown:
        raise ValueError(f"edge.reference_books references unknown bookmakers: {sorted(unknown)}")

    return Config(root=root, settings=settings, competitions=competitions, bookmakers=bookmakers)


@cache
def get_config() -> Config:
    return load_config()
