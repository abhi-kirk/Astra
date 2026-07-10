"""Unit tests for src/agent_broker.py pure helpers + token storage (no network)."""

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_broker import (
    EncryptedFileTokenStorage,
    _parse_result,
    _unwrap,
    extract_order,
    extract_portfolio,
    extract_positions,
)


class TestParseResult:
    def test_structured_preferred(self):
        r = SimpleNamespace(isError=False, structuredContent={"ok": 1}, content=[])
        assert _parse_result(r) == {"ok": 1}

    def test_json_text_fallback(self):
        r = SimpleNamespace(isError=False, structuredContent=None,
                            content=[SimpleNamespace(text='{"a": 2}')])
        assert _parse_result(r) == {"a": 2}

    def test_error_raises(self):
        from src.agent_broker import BrokerError
        r = SimpleNamespace(isError=True, structuredContent=None,
                            content=[SimpleNamespace(text="nope")])
        with pytest.raises(BrokerError):
            _parse_result(r)


class TestUnwrap:
    def test_unwraps_data_guide_envelope(self):
        assert _unwrap({"data": {"x": 1}, "guide": "hint"}) == {"x": 1}

    def test_leaves_plain_payload(self):
        assert _unwrap({"x": 1}) == {"x": 1}

    def test_parse_result_unwraps(self):
        r = SimpleNamespace(isError=False, structuredContent={"data": {"y": 2}, "guide": "h"}, content=[])
        assert _parse_result(r) == {"y": 2}


class TestExtractPortfolioPositions:
    def test_portfolio_flat(self):
        p = extract_portfolio({"total_equity": "1000", "buying_power": "950", "cash": "200"})
        assert p == {"total_equity": 1000.0, "buying_power": 950.0, "cash": 200.0}

    def test_portfolio_real_shape(self):
        # The real RH payload (post data-unwrap): total_value + nested buying_power object.
        p = extract_portfolio({
            "total_value": "1000", "cash": "1000",
            "buying_power": {"buying_power": "1000.0000", "unleveraged_buying_power": "1000.0000"},
        })
        assert p == {"total_equity": 1000.0, "buying_power": 1000.0, "cash": 1000.0}

    def test_positions(self):
        pos = extract_positions({"positions": [
            {"symbol": "rklb", "quantity": "5", "average_buy_price": "90"},
            {"symbol": "ZERO", "quantity": "0", "average_buy_price": "1"},  # dropped
        ]})
        assert pos == {"RKLB": {"shares": 5.0, "sellable": 5.0, "avg_cost": 90.0, "num_buys": 1}}

    def test_positions_sellable_settled_only(self):
        pos = extract_positions({"positions": [
            {"symbol": "RKLB", "quantity": "5", "average_buy_price": "90", "shares_available_for_sells": "3"},
        ]})
        assert pos["RKLB"]["shares"] == 5.0 and pos["RKLB"]["sellable"] == 3.0


class TestExtractOrder:
    def test_top_level_id_and_state(self):
        # The real order object (post data-unwrap) carries a top-level id + state.
        resp = {"id": "abc-123", "state": "filled", "symbol": "NVDA",
                "dollar_based_amount": {"amount": "149.86"}}
        assert extract_order(resp) == ("abc-123", "filled")

    def test_nested_under_order(self):
        assert extract_order({"order": {"id": "o-1", "state": "confirmed"}}) == ("o-1", "confirmed")

    def test_nested_orders_list(self):
        assert extract_order({"orders": [{"id": "o-2", "state": "queued"}]}) == ("o-2", "queued")

    def test_order_id_alias(self):
        assert extract_order({"order_id": "o-3", "status": "filled"}) == ("o-3", "filled")

    def test_missing_falls_back(self):
        assert extract_order({"foo": "bar"}) == (None, "submitted")

    def test_non_dict_falls_back(self):
        assert extract_order("boom") == (None, "submitted")


class TestTokenStorage:
    def test_encrypted_roundtrip_and_at_rest(self, tmp_path):
        from mcp.shared.auth import OAuthToken

        key = base64.b64encode(os.urandom(32)).decode()
        path = str(tmp_path / "agent_tokens.enc")

        s = EncryptedFileTokenStorage(path=path, key_b64=key)
        asyncio.run(s.set_tokens(OAuthToken(access_token="secret-abc", token_type="Bearer",
                                            refresh_token="refresh-xyz")))

        # A fresh instance decrypts the persisted file (headless-run path).
        got = asyncio.run(EncryptedFileTokenStorage(path=path, key_b64=key).get_tokens())
        assert got is not None
        assert got.access_token == "secret-abc" and got.refresh_token == "refresh-xyz"

        # The token is encrypted at rest — never appears in the file plaintext.
        assert "secret-abc" not in Path(path).read_text()

    def test_missing_file_returns_none(self, tmp_path):
        key = base64.b64encode(os.urandom(32)).decode()
        s = EncryptedFileTokenStorage(path=str(tmp_path / "nope.enc"), key_b64=key)
        assert asyncio.run(s.get_tokens()) is None
        assert asyncio.run(s.get_client_info()) is None


class TestOAuthRedirectCapture:
    def test_ignores_preconnect_and_captures_code(self):
        import threading
        import time
        import urllib.request

        from src.agent_broker import _wait_for_redirect

        port = 8791  # uncommon, avoids collision with the real 8765
        result: dict = {}

        def run():
            result["out"] = _wait_for_redirect(port, timeout=10)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.4)  # let the server bind

        # A browser preconnect / favicon hit must NOT complete the flow (served 204).
        urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico").read()
        # The real redirect carries the code → completes.
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state=xyz").read()

        t.join(5)
        assert result["out"] == ("abc", "xyz")
