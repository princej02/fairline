"""Parse a football-data.co.uk season CSV into normalised Polars frames.

Format notes (see https://www.football-data.co.uk/notes.txt):
- Dates are dd/mm/yy in older files and dd/mm/yyyy in newer ones.
- `Time` (UK local kick-off) only exists from 2019/20. Without it we default to 15:00 UK
  and set kickoff_time_known = False.
- Pre-match odds columns (e.g. B365H) were collected before the match, typically on Friday
  afternoon for weekend games and Tuesday afternoon for midweek games. The exact time is not
  published, so we store ts = kickoff - 2h with ts_estimated = True. That is a conservative
  upper bound: the price was available at least that early, so a backtest that bets at
  T-2h or later never sees a price it could not have had.
- Closing columns (e.g. PSCH, B365C>2.5) are stored with ts = kickoff and is_closing = True.
- Columns vary by season. We only read the odds columns that are present.

Sky Bet and Paddy Power are not in these files; they will come from The Odds API.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import polars as pl

UK_TZ = "Europe/London"
DEFAULT_KICKOFF = "15:00"
PRE_CLOSE_OFFSET = pl.duration(hours=2)
MIN_PRICE = 1.01


@dataclass(frozen=True)
class OddsColumn:
    column: str
    bookmaker: str
    market: str  # '1X2' | 'OU'
    line: float | None
    selection: str
    is_closing: bool


# (pre-match prefix, closing prefix, bookmaker code). Order is priority when two columns
# map to the same key (e.g. modern 'Max' vs legacy 'BbMx' -> market_max).
_1X2_PREFIXES: list[tuple[str, str | None, str]] = [
    ("PS", "PSC", "pinnacle"),
    ("BFE", "BFEC", "betfair_ex_uk"),
    ("B365", "B365C", "bet365"),
    ("WH", "WHC", "williamhill"),
    ("Max", "MaxC", "market_max"),
    ("Avg", "AvgC", "market_avg"),
    ("BbMx", None, "market_max"),
    ("BbAv", None, "market_avg"),
]
_OU25_PREFIXES: list[tuple[str, str | None, str]] = [
    ("P", "PC", "pinnacle"),
    ("BFE", "BFEC", "betfair_ex_uk"),
    ("B365", "B365C", "bet365"),
    ("Max", "MaxC", "market_max"),
    ("Avg", "AvgC", "market_avg"),
    ("BbMx", None, "market_max"),
    ("BbAv", None, "market_avg"),
]


def odds_columns() -> list[OddsColumn]:
    cols: list[OddsColumn] = []
    for pre, close, book in _1X2_PREFIXES:
        for prefix, closing in ((pre, False), (close, True)):
            if prefix is None:
                continue
            for suffix, sel in (("H", "home"), ("D", "draw"), ("A", "away")):
                cols.append(OddsColumn(f"{prefix}{suffix}", book, "1X2", None, sel, closing))
    for pre, close, book in _OU25_PREFIXES:
        for prefix, closing in ((pre, False), (close, True)):
            if prefix is None:
                continue
            for suffix, sel in ((">2.5", "over"), ("<2.5", "under")):
                cols.append(OddsColumn(f"{prefix}{suffix}", book, "OU", 2.5, sel, closing))
    return cols


ODDS_COLUMNS = odds_columns()

_STAT_COLUMNS = {
    "HS": "home_shots", "AS": "away_shots", "HST": "home_shots_on_target", "AST": "away_shots_on_target",
    "HF": "home_fouls", "AF": "away_fouls", "HC": "home_corners", "AC": "away_corners",
    "HY": "home_yellows", "AY": "away_yellows", "HR": "home_reds", "AR": "away_reds",
}  # fmt: skip


@dataclass
class ParsedSeason:
    events: pl.DataFrame  # one row per match
    odds: pl.DataFrame  # long format, one row per (match, book, market, selection, closing)
    warnings: list[str]


def read_raw(raw: bytes) -> pl.DataFrame:
    df = pl.read_csv(
        io.BytesIO(raw),
        infer_schema=False,  # everything as String; we cast explicitly
        encoding="utf8-lossy",
        truncate_ragged_lines=True,
    )
    df = df.rename({c: c.replace("﻿", "").strip() for c in df.columns})
    keep = [c for c in df.columns if c and not c.startswith("_duplicated")]
    df = df.select(keep)
    for required in ("Date", "HomeTeam", "AwayTeam"):
        if required not in df.columns:
            raise ValueError(f"CSV is missing required column {required!r}; got {df.columns[:10]}...")
    return df.filter(
        pl.col("HomeTeam").str.strip_chars().fill_null("") != "",
        pl.col("AwayTeam").str.strip_chars().fill_null("") != "",
    )


def _int(col: str, df: pl.DataFrame) -> pl.Expr:
    if col not in df.columns:
        return pl.lit(None, dtype=pl.Int16)
    return pl.col(col).str.strip_chars().cast(pl.Float64, strict=False).cast(pl.Int16, strict=False)


def parse_season(raw: bytes, division: str, season: str) -> ParsedSeason:
    df = read_raw(raw)
    warnings: list[str] = []

    date_str = pl.col("Date").str.strip_chars()
    date = (
        pl.when(date_str.str.len_chars() == 8)
        .then(date_str.str.strptime(pl.Date, "%d/%m/%y", strict=False))
        .otherwise(date_str.str.strptime(pl.Date, "%d/%m/%Y", strict=False))
    )
    has_time = "Time" in df.columns
    time_str = pl.col("Time").str.strip_chars().replace("", None) if has_time else pl.lit(None, dtype=pl.String)
    kickoff_local = pl.concat_str(
        [date.dt.strftime("%Y-%m-%d"), time_str.fill_null(DEFAULT_KICKOFF)], separator=" "
    ).str.strptime(pl.Datetime("us"), "%Y-%m-%d %H:%M", strict=False)

    events = df.with_row_index("row").select(
        pl.col("row"),
        pl.lit(division).alias("division"),
        pl.lit(season).alias("season"),
        date.alias("match_date"),
        kickoff_local.dt.replace_time_zone(UK_TZ, ambiguous="earliest", non_existent="null")
        .dt.convert_time_zone("UTC")
        .alias("kickoff_utc"),
        time_str.is_not_null().alias("kickoff_time_known"),
        pl.col("HomeTeam").str.strip_chars().alias("home"),
        pl.col("AwayTeam").str.strip_chars().alias("away"),
        _int("FTHG", df).alias("home_goals"),
        _int("FTAG", df).alias("away_goals"),
        _int("HTHG", df).alias("ht_home"),
        _int("HTAG", df).alias("ht_away"),
        pl.struct(
            [_int(src, df).alias(dst) for src, dst in _STAT_COLUMNS.items()]
            + [
                (pl.col("Referee").str.strip_chars() if "Referee" in df.columns else pl.lit(None, pl.String)).alias(
                    "referee"
                )
            ]
        ).alias("stats"),
    )

    bad = events.filter(pl.col("kickoff_utc").is_null())
    if bad.height:
        warnings.append(f"dropped {bad.height} rows with unparseable date/time: {bad['row'].to_list()[:5]}")
        events = events.filter(pl.col("kickoff_utc").is_not_null())

    dupes = events.group_by("match_date", "home", "away").len().filter(pl.col("len") > 1)
    if dupes.height:
        raise ValueError(f"duplicate fixtures in {division} {season}: {dupes.head(3).to_dicts()}")

    events = events.with_columns(
        pl.format("{}:{}:{}:{}:{}", pl.col("division"), pl.col("season"), pl.col("match_date"), "home", "away").alias(
            "external_id"
        ),
        pl.when(pl.col("home_goals").is_not_null() & pl.col("away_goals").is_not_null())
        .then(pl.lit("finished"))
        .otherwise(pl.lit("scheduled"))
        .alias("status"),
    )

    odds = _parse_odds(df.with_row_index("row"), events)
    warnings.extend(_overround_warnings(odds))
    return ParsedSeason(events=events, odds=odds, warnings=warnings)


def _parse_odds(df: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    present = [c for c in ODDS_COLUMNS if c.column in df.columns]
    schema = {
        "row": pl.UInt32, "price": pl.Float64, "bookmaker": pl.String, "market": pl.String,
        "line": pl.Float64, "selection": pl.String, "is_closing": pl.Boolean, "priority": pl.Int32,
    }  # fmt: skip
    if not present:
        long = pl.DataFrame(schema=schema)
    else:
        long = pl.concat(
            [
                df.select(
                    pl.col("row"),
                    pl.col(c.column).str.strip_chars().cast(pl.Float64, strict=False).alias("price"),
                    pl.lit(c.bookmaker).alias("bookmaker"),
                    pl.lit(c.market).alias("market"),
                    pl.lit(c.line, dtype=pl.Float64).alias("line"),
                    pl.lit(c.selection).alias("selection"),
                    pl.lit(c.is_closing).alias("is_closing"),
                    pl.lit(i, dtype=pl.Int32).alias("priority"),
                )
                for i, c in enumerate(present)
            ]
        )

    key = ["row", "bookmaker", "market", "line", "selection", "is_closing"]
    long = (
        long.filter(pl.col("price").is_not_null() & (pl.col("price") >= MIN_PRICE))
        .sort("priority")
        .unique(subset=key, keep="first", maintain_order=True)
        .drop("priority")
    )
    return (
        long.join(events.select("row", "external_id", "kickoff_utc"), on="row", how="inner")
        .with_columns(
            pl.when(pl.col("is_closing"))
            .then(pl.col("kickoff_utc"))
            .otherwise(pl.col("kickoff_utc") - PRE_CLOSE_OFFSET)
            .alias("ts"),
            pl.lit(True).alias("ts_estimated"),
        )
        .drop("row", "kickoff_utc")
        .sort("external_id", "bookmaker", "market", "is_closing", "selection")
    )


def _overround_warnings(odds: pl.DataFrame, lo: float = 0.98, hi: float = 1.25) -> list[str]:
    """Flag complete markets whose implied-probability sum is implausible (likely data errors)."""
    if odds.is_empty():
        return []
    n_sel = {"1X2": 3, "OU": 2}
    books = (
        odds.group_by("external_id", "bookmaker", "market", "is_closing")
        .agg(pl.len().alias("n"), (1 / pl.col("price")).sum().alias("overround"))
        .filter(pl.col("n") == pl.col("market").replace_strict(n_sel, default=0))
        .filter(~pl.col("bookmaker").is_in(["market_max", "market_avg"]))  # max can be < 1 legitimately
    )
    out = books.filter((pl.col("overround") < lo) | (pl.col("overround") > hi))
    if out.is_empty():
        return []
    return [f"{out.height} markets with overround outside [{lo}, {hi}]: {out.head(3).to_dicts()}"]
