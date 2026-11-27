from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from fairline.ingest.football_data_co_uk.parse import parse_season
from fairline.ingest.football_data_co_uk.source import season_path_code

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def modern():
    return parse_season((FIX / "E0_2425_sample.csv").read_bytes(), "E0", "2024-25")


@pytest.fixture
def legacy():
    return parse_season((FIX / "E0_1516_sample.csv").read_bytes(), "E0", "2015-16")


def test_season_path_code():
    assert season_path_code("2024-25") == "2425"
    assert season_path_code("1999-00") == "9900"


def test_events_drop_blank_rows_and_strip_bom(modern):
    assert modern.events.height == 2
    assert modern.events["home"].to_list() == ["Man United", "Arsenal"]


def test_kickoff_converted_from_uk_time_to_utc(modern):
    ko = modern.events["kickoff_utc"].to_list()
    assert ko[0] == datetime(2024, 8, 16, 19, 0, tzinfo=UTC)  # 20:00 BST
    assert ko[1] == datetime(2024, 12, 14, 15, 0, tzinfo=UTC)  # 15:00 GMT
    assert modern.events["kickoff_time_known"].all()


def test_legacy_two_digit_year_and_default_kickoff(legacy):
    ev = legacy.events
    assert ev["kickoff_utc"][0] == datetime(2015, 8, 8, 14, 0, tzinfo=UTC)  # 15:00 BST default
    assert not ev["kickoff_time_known"].any()
    assert ev["status"].to_list() == ["finished", "finished"]
    assert ev["stats"][0]["home_shots"] == 11
    assert ev["stats"][0]["home_corners"] is None


def test_odds_long_format(modern):
    o = modern.odds
    mu = o.filter(pl.col("external_id").str.contains("Man United"))
    # 6 books x 1X2 x (pre+close) = 36, plus OU: 4 books x 2 sel x (pre+close) = 16
    assert mu.height == 36 + 16
    pin_close = mu.filter(
        (pl.col("bookmaker") == "pinnacle") & pl.col("is_closing") & (pl.col("market") == "1X2")
    ).sort("selection")
    assert pin_close["selection"].to_list() == ["away", "draw", "home"]
    assert pin_close["price"].to_list() == [6.40, 4.70, 1.55]


def test_timestamps_conservative(modern):
    o = modern.odds.filter(pl.col("external_id").str.contains("Man United"))
    kickoff = datetime(2024, 8, 16, 19, 0, tzinfo=UTC)
    assert o.filter(pl.col("is_closing"))["ts"].unique().to_list() == [kickoff]
    pre = o.filter(~pl.col("is_closing"))["ts"].unique().to_list()
    assert pre == [datetime(2024, 8, 16, 17, 0, tzinfo=UTC)]
    assert o["ts_estimated"].all()


def test_missing_price_is_skipped(modern):
    ars = modern.odds.filter(
        pl.col("external_id").str.contains("Arsenal")
        & (pl.col("bookmaker") == "bet365")
        & (pl.col("market") == "OU")
        & pl.col("is_closing")
    )
    assert ars["selection"].to_list() == ["under"]


def test_legacy_betbrain_maps_to_aggregates(legacy):
    books = set(legacy.odds["bookmaker"].unique())
    assert books == {"bet365", "pinnacle", "market_max", "market_avg"}
    assert not legacy.odds["is_closing"].any()


def test_no_duplicate_keys(modern, legacy):
    key = ["external_id", "bookmaker", "market", "line", "selection", "is_closing"]
    for p in (modern, legacy):
        assert p.odds.select(key).is_duplicated().sum() == 0


def test_duplicate_fixture_raises():
    raw = (FIX / "E0_1516_sample.csv").read_text().splitlines()
    dup = "\n".join([*raw, raw[1]]).encode()
    with pytest.raises(ValueError, match="duplicate fixtures"):
        parse_season(dup, "E0", "2015-16")


def test_overround_sanity_no_warnings_on_clean_data(modern):
    assert modern.warnings == []
