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
                "conviction": "high",
                "approved": ["GOOGL", "NVDA"],
                "preferred": [],
                "hold_only": [],
                "do_not_add": [],
                "notes": {},
            },
            "space": {
                "conviction": "very_high",
                "approved": ["RKLB", "ASTS"],
                "preferred": [],
                "hold_only": [],
                "do_not_add": ["SPCE"],
                "notes": {},
            },
        },
        "ticker_metadata": {
            "RKLB": {"intent": "thesis_hold",   "original_catalyst": None},
            "ASTS": {"intent": "thesis_hold",   "original_catalyst": None},
            "NVDA": {"intent": "thesis_hold",   "original_catalyst": None},
            "GOOGL":{"intent": "thesis_hold",   "original_catalyst": None},
            "NIO":  {"intent": "written_off",   "original_catalyst": None},
            "SPCE": {"intent": "written_off",   "original_catalyst": None},
            "DAL":  {"intent": "opportunistic", "original_catalyst": "COVID airline recovery"},
        },
    }


@pytest.fixture
def good_market_data():
    """A strong BUY setup: sound fundamentals + an uptrend pullback to the rising 50-MA."""
    return {
        "current_price": 85.0,
        "revenue_growth_yoy": 0.25,
        "gross_margins": 0.55,
        "debt_to_equity": 40.0,
        "current_ratio": 1.6,
        "free_cashflow": 500_000_000,
        "peg_ratio": 1.0,
        "forward_pe": 15.0,
        "pct_below_52w_high": 20.0,
        "rsi_14": 35.0,
        "price_vs_ma50_pct": -3.0,
        # Brain technicals — uptrend (price > rising 200-MA, 50-MA ≥ 200-MA), mild pullback
        "ma_50": 87.6,
        "ma_200": 70.0,
        "atr_14": 3.0,
        "recent_swing_high": 95.0,
        "mom_12_1": 0.30,
        "revisions": {"up": 4, "down": 1},
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
