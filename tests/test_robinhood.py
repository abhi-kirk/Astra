"""
Tests for src/robinhood.py — pure functions only (no credentials needed).
"""

import base64
import json
import os
from unittest.mock import patch

import pytest

from src import config
from src.robinhood import (
    _enrich_num_buys,
    _fetch_buying_power,
    _positions_to_portfolio,
)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestFetchBuyingPower:
    def test_reads_buying_power(self, monkeypatch):
        monkeypatch.setattr("src.robinhood.requests.get",
                            lambda *a, **k: _Resp({"results": [{"buying_power": "13785.57", "cash": "0"}]}))
        assert _fetch_buying_power("tok") == pytest.approx(13785.57)

    def test_falls_back_to_cash_fields(self, monkeypatch):
        monkeypatch.setattr("src.robinhood.requests.get",
                            lambda *a, **k: _Resp({"results": [{"buying_power": "", "portfolio_cash": "500.00"}]}))
        assert _fetch_buying_power("tok") == pytest.approx(500.0)

    def test_empty_results_returns_none(self, monkeypatch):
        monkeypatch.setattr("src.robinhood.requests.get", lambda *a, **k: _Resp({"results": []}))
        assert _fetch_buying_power("tok") is None

    def test_request_error_returns_none(self, monkeypatch):
        import requests

        def _boom(*a, **k):
            raise requests.RequestException("network down")
        monkeypatch.setattr("src.robinhood.requests.get", _boom)
        assert _fetch_buying_power("tok") is None

# ---------------------------------------------------------------------------
# _positions_to_portfolio
# ---------------------------------------------------------------------------

class TestPositionsToPortfolio:
    def test_maps_symbol_qty_avg(self):
        positions = [
            {"symbol": "RKLB", "quantity": "98.5", "average_buy_price": "109.51"},
            {"symbol": "ASTS", "quantity": "50.0", "average_buy_price": "25.00"},
        ]
        result = _positions_to_portfolio(positions)
        assert set(result.keys()) == {"RKLB", "ASTS"}
        assert result["RKLB"]["shares"] == pytest.approx(98.5)
        assert result["RKLB"]["avg_cost"] == pytest.approx(109.51)
        assert result["ASTS"]["shares"] == pytest.approx(50.0)

    def test_skips_zero_quantity(self):
        positions = [
            {"symbol": "RKLB", "quantity": "0", "average_buy_price": "109.51"},
            {"symbol": "ASTS", "quantity": "50.0", "average_buy_price": "25.00"},
        ]
        result = _positions_to_portfolio(positions)
        assert "RKLB" not in result
        assert "ASTS" in result

    def test_skips_empty_symbol(self):
        positions = [
            {"symbol": "", "quantity": "10.0", "average_buy_price": "50.00"},
        ]
        result = _positions_to_portfolio(positions)
        assert result == {}

    def test_handles_none_avg_price(self):
        positions = [
            {"symbol": "RKLB", "quantity": "10.0", "average_buy_price": None},
        ]
        result = _positions_to_portfolio(positions)
        assert result["RKLB"]["avg_cost"] == pytest.approx(0.0)

    def test_normalises_symbol_to_upper(self):
        positions = [{"symbol": "rklb", "quantity": "5.0", "average_buy_price": "100.0"}]
        result = _positions_to_portfolio(positions)
        assert "RKLB" in result

    def test_default_num_buys_is_1(self):
        positions = [{"symbol": "NVDA", "quantity": "10.0", "average_buy_price": "500.0"}]
        result = _positions_to_portfolio(positions)
        assert result["NVDA"]["num_buys"] == 1

    def test_empty_list(self):
        assert _positions_to_portfolio([]) == {}


# ---------------------------------------------------------------------------
# _enrich_num_buys
# ---------------------------------------------------------------------------

class TestEnrichNumBuys:
    def test_merges_num_buys_from_db(self):
        portfolio = {
            "RKLB": {"shares": 10.0, "avg_cost": 100.0, "num_buys": 1},
            "ASTS": {"shares": 5.0,  "avg_cost": 25.0,  "num_buys": 1},
        }
        db_data = {
            "RKLB": {"num_buys": 7, "shares": 10.0, "avg_cost": 100.0},
            "ASTS": {"num_buys": 3, "shares": 5.0, "avg_cost": 25.0},
        }
        with patch("src.data_layer.get_cost_basis_from_db", return_value=db_data):
            _enrich_num_buys(portfolio)
        assert portfolio["RKLB"]["num_buys"] == 7
        assert portfolio["ASTS"]["num_buys"] == 3

    def test_defaults_to_1_when_ticker_missing_in_db(self):
        portfolio = {"NVDA": {"shares": 10.0, "avg_cost": 500.0, "num_buys": 1}}
        with patch("src.data_layer.get_cost_basis_from_db", return_value={}):
            _enrich_num_buys(portfolio)
        assert portfolio["NVDA"]["num_buys"] == 1

    def test_does_not_raise_if_db_errors(self):
        portfolio = {"RKLB": {"shares": 10.0, "avg_cost": 100.0, "num_buys": 1}}
        with patch("src.data_layer.get_cost_basis_from_db", side_effect=Exception("DB down")):
            _enrich_num_buys(portfolio)  # must not raise
        assert portfolio["RKLB"]["num_buys"] == 1


# ---------------------------------------------------------------------------
# _decrypt_tokens (integration-light: tests the crypto path with a real key)
# ---------------------------------------------------------------------------

class TestDecryptTokens:
    def test_decrypt_round_trip(self, monkeypatch, tmp_path):
        """Encrypt a known payload and verify decrypt_tokens recovers it."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = os.urandom(32)
        iv  = os.urandom(12)
        payload = json.dumps({"access_token": "tok_abc", "refresh_token": "ref_xyz"}).encode()
        aesgcm = AESGCM(key)
        ct_with_tag = aesgcm.encrypt(iv, payload, None)
        ct  = ct_with_tag[:-16]
        tag = ct_with_tag[-16:]

        enc = {
            "iv":         base64.b64encode(iv).decode(),
            "tag":        base64.b64encode(tag).decode(),
            "ciphertext": base64.b64encode(ct).decode(),
        }
        tokens_file = tmp_path / "tokens.enc"
        tokens_file.write_text(json.dumps(enc))

        monkeypatch.setattr(config.robinhood, "token_key",   base64.b64encode(key).decode())
        monkeypatch.setattr(config.robinhood, "tokens_file", str(tokens_file))

        from src.robinhood import _decrypt_tokens
        result = _decrypt_tokens()
        assert result["access_token"] == "tok_abc"
        assert result["refresh_token"] == "ref_xyz"

    def test_raises_if_key_missing(self, monkeypatch):
        monkeypatch.setattr(config.robinhood, "token_key", "")
        from src.robinhood import _decrypt_tokens
        with pytest.raises(RuntimeError, match="ROBINHOOD_TOKEN_KEY not set"):
            _decrypt_tokens()

    def test_raises_if_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config.robinhood, "token_key",   "dGVzdA==")
        monkeypatch.setattr(config.robinhood, "tokens_file", str(tmp_path / "no_file.enc"))
        from src.robinhood import _decrypt_tokens
        with pytest.raises(RuntimeError, match="not found"):
            _decrypt_tokens()
