"""
Shared daemon-thread timeout utility for Claude API calls.

Using daemon threads (not signal.SIGALRM or ThreadPoolExecutor) because:
- SIGALRM only works on the main thread
- ThreadPoolExecutor.shutdown(wait=True) blocks even after .result(timeout=...) returns
- Daemon threads are abandoned when the main thread exits, giving a true wall-clock kill
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


def run_with_timeout(fn: Callable[[], T], timeout_sec: int, label: str = "call") -> T:
    """
    Run fn() in a daemon thread with a hard wall-clock timeout.

    If fn() exceeds timeout_sec the thread is abandoned (killed on process exit)
    and TimeoutError is raised. Exceptions from fn() are re-raised in the caller.
    """
    result: list = [None]
    error: list = [None]

    def _run() -> None:
        try:
            result[0] = fn()
        except Exception as exc:
            error[0] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        raise TimeoutError(f"{label} exceeded {timeout_sec}s wall-clock limit — thread abandoned")
    if error[0]:
        raise error[0]
    return result[0]
