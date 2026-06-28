import pytest


@pytest.fixture
def convictions():
    return {
        "exclusions": [{"ticker": "TSLA", "reason": "Employment blackout"}],
        "individual_holdings": {
            "NIO": {"status": "hold", "thesis": "hold only, no new buys"},
        },
        "themes": {
            "core_tech": {
                "approved": ["GOOGL", "NVDA"],
                "preferred": [],
                "hold_only": [],
                "do_not_add": [],
                "notes": {},
            },
            "space": {
                "approved": ["RKLB", "ASTS"],
                "preferred": [],
                "hold_only": [],
                "do_not_add": ["SPCE"],
                "notes": {},
            },
        },
    }


@pytest.fixture
def good_market_data():
    """Passes both quality filter and technical signal."""
    return {
        "current_price": 85.0,
        "revenue_growth_yoy": 0.25,
        "gross_margins": 0.55,
        "debt_to_equity": 40.0,
        "free_cashflow": 500_000_000,
        "pct_below_52w_high": 20.0,
        "rsi_14": 35.0,
        "price_vs_ma50_pct": -12.0,
    }


@pytest.fixture
def base_position():
    return {"shares": 100.0, "avg_cost": 100.0, "num_buys": 1}


@pytest.fixture
def portfolio_summary():
    return {
        "total_value": 100_000.0,
        "theme_allocations": {"space": 0.10, "core_tech": 0.35},
    }
