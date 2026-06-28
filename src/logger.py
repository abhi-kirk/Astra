"""
Centralised logging setup for ASTRA.

Call setup() once at each entry point (__main__). All other modules
use logging.getLogger(__name__) — no direct import of this module needed.

Log levels:
  DEBUG    — verbose per-ticker detail, loop internals (off by default)
  INFO     — normal run flow: phase transitions, counts, decisions saved
  WARNING  — unexpected but handled: missing data, skipped tickers, degraded mode
  ERROR    — operation failed but run continues; always includes traceback
  CRITICAL — fatal: run cannot proceed
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Generator

_FMT  = "%(asctime)s  %(levelname)-8s  %(name)-24s  %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def setup(level: str = "INFO") -> None:
    """
    Configure the root logger with stdout + rotating file handlers.
    Safe to call multiple times — exits immediately if already configured.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(level)
    fmt = logging.Formatter(_FMT, datefmt=_DATE)

    # stdout — captured verbatim by GitHub Actions workflow logs
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file — local dev only (ephemeral on GitHub Actions runners)
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / "astra.log",
        maxBytes=10_000_000,   # 10 MB per file
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "hpack", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def timer(label: str, log: logging.Logger | None = None) -> Generator[None, None, None]:
    """Context manager that logs elapsed time for a named operation."""
    _log = log or logging.getLogger(__name__)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        _log.info(f"TIMING  {label:<40}  {elapsed:.2f}s")
