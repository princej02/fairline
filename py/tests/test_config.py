import pytest

from fairline.core.config import NotAllowed, get_config, season_range


def test_settings_defaults():
    e = get_config().settings.edge
    assert (e.kelly_fraction, e.max_stake_pct, e.min_ev, e.max_odds) == (0.25, 0.02, 0.03, 6.0)
    assert e.reference_books == ["pinnacle", "betfair_ex_uk"]


def test_allowlist_rejects_unknown_competition():
    with pytest.raises(NotAllowed):
        get_config().competition("ITA1")  # Serie A is deliberately out of scope


def test_provider_mapping_required():
    with pytest.raises(NotAllowed):
        get_config().competition("UCL").provider_code("football_data_co_uk")


def test_phase1_leagues_for_fdcuk():
    codes = {c.code for c in get_config().competitions_for("football_data_co_uk")}
    assert codes == {"ENG1", "ENG2", "ESP1", "ESP2", "GER1", "GER2", "FRA1", "FRA2"}


def test_season_range():
    assert season_range("2023-24", "2025-26") == ["2023-24", "2024-25", "2025-26"]
    with pytest.raises(ValueError):
        season_range("2023-25", "2025-26")


def test_odds_api_books_exclude_bet365_and_fit_one_region():
    from fairline.core.config import get_config

    books = get_config().provider_books("the_odds_api")
    assert "bet365" not in books  # historical-only via football-data.co.uk
    assert {"pinnacle", "betfair_ex_uk"} <= books.keys()  # both sharp references are live
    assert "market_max" not in books and len(books) <= 10  # <= 10 books bill as one region
