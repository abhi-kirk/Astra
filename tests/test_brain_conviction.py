"""Unit tests for src/brain/conviction.buyable_tickers — the daily screening universe."""

from src.brain.conviction import buyable_tickers


def test_buyable_is_preferred_and_approved_only(convictions):
    # approved: GOOGL, NVDA (core_tech) + RKLB, ASTS (space)
    assert buyable_tickers(convictions) == ["ASTS", "GOOGL", "NVDA", "RKLB"]


def test_hold_only_and_do_not_add_are_excluded(convictions):
    buyable = buyable_tickers(convictions)
    assert "NIO" not in buyable    # individual holding, status=hold → hold-only
    assert "SPCE" not in buyable   # space do_not_add


def test_hard_excluded_never_buyable(convictions):
    convictions["themes"]["core_tech"]["approved"].append("TSLA")
    assert "TSLA" not in buyable_tickers(convictions)


def test_preferred_bucket_included(convictions):
    convictions["themes"]["core_tech"]["preferred"] = ["MSFT"]
    assert "MSFT" in buyable_tickers(convictions)


def test_empty_convictions_yields_nothing():
    assert buyable_tickers({}) == []
