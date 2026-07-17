"""
Unit tests for the pure performance-measurement helpers in src/performance.py.

Covers the two methodologies feeding the dashboard charts: Modified Dietz TWR (Autotrader,
which takes deposits) and the notional paper book (no external flows), incl. the new-lot /
add / trim / full-close / missing-price reconciliation paths.
"""

import pytest

from src.performance import chain_nav, dietz_return, reconcile_notional_book

# per-name cap 100% and default 4% so tests exercise sizing without the cap biting unless asked
NAV0 = 10_000.0
MAXPCT = 1.0
DEFPCT = 0.04


class TestDietzReturn:
    def test_no_flow_is_simple_return(self):
        assert dietz_return(1000, 1100, 0) == pytest.approx(0.10)

    def test_deposit_mid_period_excluded_from_return(self):
        # +100 deposited mid-period: gain is (1150-1000-100)=50 on base (1000+0.5*100)=1050.
        assert dietz_return(1000, 1150, 100) == pytest.approx(50 / 1050)

    def test_withdrawal_negative_flow(self):
        # -100 withdrawn: (900-1000+100)=0 gain → flat despite equity dropping.
        assert dietz_return(1000, 900, -100) == pytest.approx(0.0)

    def test_zero_capital_base_guarded(self):
        assert dietz_return(0, 0, 0) == 0.0
        # V0 + 0.5*CF == 0 → guarded, no ZeroDivisionError
        assert dietz_return(100, 50, -200) == 0.0


class TestChainNav:
    def test_links_returns_geometrically(self):
        nav = chain_nav(100.0, 0.10)
        assert nav == pytest.approx(110.0)
        assert chain_nav(nav, -0.05) == pytest.approx(104.5)


def _lot(lot_id, ticker, shares, price, pct=DEFPCT):
    return {"id": lot_id, "ticker": ticker, "virtual_shares": shares,
            "price_at_signal": price, "suggested_position_pct": pct}


class TestReconcileNotionalBook:
    def test_new_lot_deploys_cash_no_equity_change_at_entry(self):
        lots = [_lot(1, "AAA", 50.0, 100.0, pct=0.04)]  # deploy 4% of 10k = $400 → 4 shares
        b = reconcile_notional_book([], lots, NAV0, {"AAA": 100.0}, NAV0, MAXPCT, DEFPCT)
        assert b["cash"] == pytest.approx(9600.0)
        assert b["market_value"] == pytest.approx(400.0)
        assert b["cash"] + b["market_value"] == pytest.approx(NAV0)  # equity flat at buy
        assert b["holdings"][0]["notional_shares"] == pytest.approx(4.0)

    def test_mark_to_market_moves_equity(self):
        holdings = [{"lot_id": 1, "ticker": "AAA", "notional_shares": 4.0,
                     "entry_price": 100.0, "virtual_shares_ref": 50.0}]
        b = reconcile_notional_book(holdings, [_lot(1, "AAA", 50.0, 100.0)], 9600.0,
                                    {"AAA": 110.0}, NAV0, MAXPCT, DEFPCT)
        assert b["market_value"] == pytest.approx(440.0)
        assert b["unrealized"] == pytest.approx(40.0)
        assert b["realized_delta"] == pytest.approx(0.0)

    def test_position_cap_limits_deploy(self):
        # pct 0.50 but cap 0.10 → deploy $1000, not $5000
        lots = [_lot(1, "AAA", 1.0, 100.0, pct=0.50)]
        b = reconcile_notional_book([], lots, NAV0, {"AAA": 100.0}, NAV0, 0.10, DEFPCT)
        assert b["cash"] == pytest.approx(9000.0)
        assert b["holdings"][0]["notional_shares"] == pytest.approx(10.0)

    def test_cash_exhaustion_caps_deploy(self):
        lots = [_lot(1, "AAA", 1.0, 100.0, pct=0.50)]
        b = reconcile_notional_book([], lots, 200.0, {"AAA": 100.0}, NAV0, MAXPCT, DEFPCT)
        assert b["cash"] == pytest.approx(0.0)  # only $200 available
        assert b["holdings"][0]["notional_shares"] == pytest.approx(2.0)

    def test_full_close_realizes_into_cash(self):
        holdings = [{"lot_id": 1, "ticker": "AAA", "notional_shares": 4.0,
                     "entry_price": 100.0, "virtual_shares_ref": 50.0}]
        # lot 1 no longer open → close at 120
        b = reconcile_notional_book(holdings, [], 9600.0, {"AAA": 120.0}, NAV0, MAXPCT, DEFPCT)
        assert b["cash"] == pytest.approx(9600.0 + 4.0 * 120.0)
        assert b["realized_delta"] == pytest.approx(80.0)  # (120-100)*4
        assert b["holdings"] == []
        assert b["market_value"] == pytest.approx(0.0)

    def test_partial_trim_scales_shares_and_realizes(self):
        holdings = [{"lot_id": 1, "ticker": "AAA", "notional_shares": 4.0,
                     "entry_price": 100.0, "virtual_shares_ref": 50.0}]
        # live shares halved (50 → 25) → trim 50% at price 120
        b = reconcile_notional_book(holdings, [_lot(1, "AAA", 25.0, 100.0)], 9600.0,
                                    {"AAA": 120.0}, NAV0, MAXPCT, DEFPCT)
        assert b["realized_delta"] == pytest.approx((120 - 100) * 2.0)  # sold 2 shares
        assert b["cash"] == pytest.approx(9600.0 + 2.0 * 120.0)
        assert b["holdings"][0]["notional_shares"] == pytest.approx(2.0)
        assert b["holdings"][0]["virtual_shares_ref"] == pytest.approx(25.0)

    def test_add_lot_is_separate_holding(self):
        holdings = [{"lot_id": 1, "ticker": "AAA", "notional_shares": 4.0,
                     "entry_price": 100.0, "virtual_shares_ref": 50.0}]
        lots = [_lot(1, "AAA", 50.0, 100.0), _lot(2, "AAA", 40.0, 110.0, pct=0.04)]
        b = reconcile_notional_book(holdings, lots, 9600.0, {"AAA": 110.0}, NAV0, MAXPCT, DEFPCT)
        assert len(b["holdings"]) == 2
        # new lot 2 deploys $400 at 110 → 3.6363 shares
        add = next(h for h in b["holdings"] if h["lot_id"] == 2)
        assert add["notional_shares"] == pytest.approx(400.0 / 110.0)
        assert b["cash"] == pytest.approx(9200.0)

    def test_missing_price_holds_at_entry(self):
        holdings = [{"lot_id": 1, "ticker": "AAA", "notional_shares": 4.0,
                     "entry_price": 100.0, "virtual_shares_ref": 50.0}]
        b = reconcile_notional_book(holdings, [_lot(1, "AAA", 50.0, 100.0)], 9600.0,
                                    {}, NAV0, MAXPCT, DEFPCT)  # no price for AAA
        assert b["market_value"] == pytest.approx(400.0)  # marked at entry, no phantom move
        assert b["unrealized"] == pytest.approx(0.0)
