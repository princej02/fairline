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
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    devig_method: Literal["multiplicative", "additive", "power", "shin"]
    reference_books: list[str]
    bettable_books: list[str]
    betfair_commission: float = Field(ge=0, lt=1)
    max_match_exposure_pct: float = Field(gt=0, le=1)
    max_slate_exposure_pct: float = Field(gt=0, le=1)
    one_per_match: bool
    disagreement_warn: float = Field(gt=0, lt=1)
    conservative_quantile: float = Field(gt=0, lt=0.5)
    acca_max_legs: int = Field(ge=2)
    acca_max_combined_odds: float = Field(gt=1)


class FootballDataCoUkSettings(BaseModel):
    base_url: str
    backfill_from: str
    current_season: str
    refresh_current_hours: float
    request_delay_seconds: float
    cache_dir: str


class TheOddsApiSettings(BaseModel):
    base_url: str
    markets: str
    competitions: list[str]
    monthly_credit_budget: int = Field(gt=0)
    reserve_credits: int = Field(ge=0)
    pace_slack: float = Field(ge=0, le=1)
    warn_at: float = Field(gt=0, le=1)
    poll_windows_hours: list[float]
    window_tolerance_share: float = Field(ge=0, lt=1)
    close_window_minutes: float = Field(gt=0)
    close_max_age_minutes: float = Field(gt=0)
    events_refresh_hours: float = Field(gt=0)
    tick_minutes: float = Field(gt=0)
    cache_dir: str

    @model_validator(mode="after")
    def _close_after_window(self):
        if self.close_max_age_minutes < self.close_window_minutes + self.tick_minutes:
            raise ValueError("close_max_age_minutes must cover close_window_minutes + one tick")
        return self


class DixonColesSettings(BaseModel):
    max_goals: int = Field(ge=10, description="grid must cover at least 0-10 goals per side")
    half_life_days: float = Field(gt=0)
    max_history_days: int = Field(gt=0)
    prior_sd: float = Field(gt=0)
    rho_bounds: tuple[float, float]
    result_delay_hours: float = Field(ge=0)
    cold_start: Literal["pooled", "prior"]
    min_effective_matches: float = Field(ge=0)
    promoted_prior_quantile: float = Field(gt=0, lt=1)
    pools: dict[str, list[str]]


class WalkForwardSettings(BaseModel):
    burn_in_days: int = Field(ge=0)
    retrain_every_days: int = Field(gt=0)


class BayesSettings(BaseModel):
    history_days: int = Field(gt=0)
    draws: int = Field(gt=0)
    tune: int = Field(gt=0)
    chains: int = Field(gt=0)
    interval: float = Field(gt=0, lt=1)
    target_accept: float = Field(gt=0.5, lt=1)


class ModelSettings(BaseModel):
    dixon_coles: DixonColesSettings
    walkforward: WalkForwardSettings
    bayes: BayesSettings


class TrackerSettings(BaseModel):
    bankroll: float = Field(gt=0)
    currency: str


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    edge: EdgeSettings
    markets: dict[str, Any]
    ingest: dict[str, Any]
    models: ModelSettings
    tracker: TrackerSettings

    @property
    def fdcuk(self) -> FootballDataCoUkSettings:
        return FootballDataCoUkSettings(**self.ingest["football_data_co_uk"])

    @property
    def odds_api(self) -> TheOddsApiSettings:
        return TheOddsApiSettings(**self.ingest["the_odds_api"])


_SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")


def season_start_year(label: str) -> int:
    m = _SEASON_RE.match(label)
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise ValueError(f"bad season label {label!r}; expected e.g. '2024-25'")
    return int(m.group(1))


def season_range(first: str, last: str) -> list[str]:
    a, b = season_start_year(first), season_start_year(last)
    return [f"{y}-{(y + 1) % 100:02d}" for y in range(a, b + 1)]


def season_label_for(d) -> str:
    """European football season of a date: July-June. date(2026, 8, 1) -> '2026-27'."""
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


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

    def provider_books(self, provider: str) -> dict[str, str]:
        """Our code -> the provider's key, for books that provider actually carries. A null mapping
        (e.g. Bet365 on The Odds API) means historical-only: never request it, never expect rows."""
        return {
            code: key
            for code, b in self.bookmakers.items()
            if b.kind != "aggregate" and (key := b.providers.get(provider))
        }

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

    unknown = (set(settings.edge.reference_books) | set(settings.edge.bettable_books)) - bookmakers.keys()
    if unknown:
        raise ValueError(f"edge reference/bettable books reference unknown bookmakers: {sorted(unknown)}")
    for pool, codes in settings.models.dixon_coles.pools.items():
        bad = [c for c in codes if c not in competitions]
        if bad:
            raise ValueError(f"models.dixon_coles.pools.{pool} references non-allowlisted competitions: {bad}")
    if "the_odds_api" in settings.ingest:
        for code in settings.odds_api.competitions:
            if code not in competitions:
                raise ValueError(f"ingest.the_odds_api.competitions: {code!r} is not allowlisted")
            competitions[code].provider_code("the_odds_api")  # raises NotAllowed if unmapped

    return Config(root=root, settings=settings, competitions=competitions, bookmakers=bookmakers)


@cache
def get_config() -> Config:
    return load_config()
