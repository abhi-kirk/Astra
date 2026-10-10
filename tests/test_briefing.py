"""Daily phone note: dip selection, new-name selection, thesis-only sells, rendering."""

from datetime import datetime, timedelta, timezone

from src.briefing import (
    headline_from_items,
    known_tickers,
    pick_new_names,
    render_briefing,
    select_dips,
    thesis_breaks,
)

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _md(price, high, pct):
    return {"current_price": price, "high_3m": high, "pct_below_3m_high": pct}


class TestSelectDips:
    def test_deepest_first_and_shallow_names_stay_quiet(self):
        portfolio = {"INTC": {"shares": 10}, "MSFT": {"shares": 2}, "AMD": {"shares": 1}}
        market = {
            "INTC": _md(105, 132, 20.5),
            "MSFT": _md(535, 540, 0.9),
            "AMD": _md(500, 580, 13.8),
        }
        dips = select_dips(portfolio, market, threshold=0.12, limit=5)
        assert [d["ticker"] for d in dips] == ["INTC", "AMD"]

    def test_skips_a_name_with_no_recent_high(self):
        dips = select_dips(
            {"AAA": {"shares": 1}},
            {"AAA": {"current_price": 10, "pct_below_52w_high": 40}},
            threshold=0.12, limit=5,
        )
        assert dips == []

    def test_respects_the_cap(self):
        portfolio = {t: {"shares": 1} for t in ("A", "B", "C")}
        market = {t: _md(80, 100, 20) for t in portfolio}
        assert len(select_dips(portfolio, market, threshold=0.12, limit=2)) == 2


class TestNewNames:
    def test_skips_held_known_and_stale(self):
        candidates = [
            {"ticker": "SERV", "source_theme": "robotics", "rationale": "Humanoids. Second sentence.",
             "discovered_at": "2026-10-08T00:00:00+00:00"},
            {"ticker": "RKLB", "source_theme": "space", "rationale": "Already held.",
             "discovered_at": "2026-10-08T00:00:00+00:00"},
            {"ticker": "OLD", "source_theme": "space", "rationale": "Stale.",
             "discovered_at": "2026-09-01T00:00:00+00:00"},
            {"ticker": "NVDA", "source_theme": "core_tech", "rationale": "Named already.",
             "discovered_at": "2026-10-09T00:00:00+00:00"},
        ]
        picked = pick_new_names(
            candidates, held={"RKLB"}, known={"NVDA"},
            limit=2, now=NOW, max_age_days=8,
        )
        assert [p["ticker"] for p in picked] == ["SERV"]
        assert picked[0]["theme"] == "robotics"
        assert picked[0]["rationale"] == "Humanoids."

    def test_caps_at_two(self):
        day = (NOW - timedelta(days=1)).isoformat()
        candidates = [
            {"ticker": t, "source_theme": "space", "rationale": "New.", "discovered_at": day}
            for t in ("AAA", "BBB", "CCC")
        ]
        picked = pick_new_names(candidates, held=set(), known=set(), limit=2, now=NOW, max_age_days=8)
        assert [p["ticker"] for p in picked] == ["AAA", "BBB"]


class TestKnownAndThesis:
    def test_known_tickers_cover_buckets_and_exclusions(self):
        convictions = {
            "exclusions": [{"ticker": "TSLA", "reason": "job"}],
            "individual_holdings": {"DAL": {"status": "hold"}},
            "themes": {"space": {"approved": ["RKLB"], "do_not_add": ["SPCE"], "notes": {"ASTS": "x"}}},
        }
        assert known_tickers(convictions) == {"TSLA", "DAL", "RKLB", "SPCE", "ASTS"}

    def test_only_thesis_breaks_are_texted(self):
        signals = [
            {"ticker": "DAL", "action": "sell", "close_reason": "thesis_invalidation",
             "reasons": ["Revenue declining 12% YoY"]},
            {"ticker": "PL", "action": "sell", "close_reason": "trailing_stop",
             "reasons": ["Stopped out"]},
            {"ticker": "AMD", "action": "buy", "close_reason": None, "reasons": []},
        ]
        assert thesis_breaks(signals) == [
            {"ticker": "DAL", "reason": "Revenue declining 12% YoY"},
        ]


class TestHeadlines:
    def test_uses_the_newest_fresh_title(self):
        now = 1_700_000_000
        items = [
            {"title": "Old story.", "providerPublishTime": now - 40 * 86400},
            {"content": {"title": "Fresh story about the drop.", "pubDate": "ignore"},
             "providerPublishTime": now - 2 * 86400},
        ]
        assert headline_from_items(items, now, 21) == "Fresh story about the drop."

    def test_drops_stale_headlines(self):
        now = 1_700_000_000
        items = [{"title": "Ancient.", "providerPublishTime": now - 40 * 86400}]
        assert headline_from_items(items, now, 21) is None


class TestRender:
    def test_empty_dips_say_so_and_new_names_are_optional(self):
        text = render_briefing([], {}, [], [], threshold=0.12)
        assert "Nothing you hold is down 12%" in text
        assert "New" not in text
        assert "dashboard" not in text.lower()

    def test_dip_line_includes_headline_and_thesis(self):
        text = render_briefing(
            [{"ticker": "INTC", "drawdown_pct": 20.4, "price": 104.7, "high": 131.72}],
            {"INTC": "Intel cuts its forecast."},
            [{"ticker": "SERV", "theme": "robotics", "rationale": "Humanoids."}],
            [{"ticker": "DAL", "reason": "Revenue declining 12% YoY"}],
            threshold=0.12,
        )
        assert "INTC" in text and "20% below" in text
        assert "Intel cuts its forecast." in text
        assert "SERV" in text and "robotics" in text
        assert "DAL" in text and "Revenue declining" in text
