"""Bounded exponential retry with jitter for transient remote failures."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int,
    base_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    random_float: Callable[[], float] = random.random,
) -> T:
    """Retry an operation, using full jitter and preserving its final exception."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # callers only wrap remote I/O in this helper
            last_error = exc
            if attempt == attempts - 1:
                break
            ceiling = base_seconds * (2**attempt)
            sleeper(ceiling * random_float())
    assert last_error is not None
    raise last_error
