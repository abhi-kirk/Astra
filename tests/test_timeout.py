"""Tests for src/timeout.py — shared daemon-thread timeout utility."""

import time
import pytest

from src.timeout import run_with_timeout


class TestRunWithTimeout:
    def test_returns_result_on_success(self):
        result = run_with_timeout(lambda: 42, timeout_sec=5)
        assert result == 42

    def test_propagates_exception_from_fn(self):
        def _boom():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            run_with_timeout(_boom, timeout_sec=5)

    def test_raises_timeout_error_when_fn_hangs(self):
        def _hang():
            time.sleep(60)

        with pytest.raises(TimeoutError, match="exceeded 1s"):
            run_with_timeout(_hang, timeout_sec=1, label="hang test")

    def test_label_appears_in_timeout_message(self):
        with pytest.raises(TimeoutError, match="my label"):
            run_with_timeout(lambda: time.sleep(60), timeout_sec=1, label="my label")

    def test_returns_none_when_fn_returns_none(self):
        result = run_with_timeout(lambda: None, timeout_sec=5)
        assert result is None
