from __future__ import annotations

import pytest

from app.retry import retry_call


def test_retry_uses_bounded_exponential_backoff_and_eventually_returns():
    attempts = 0
    waits: list[float] = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return "ok"

    assert retry_call(operation, attempts=3, base_seconds=1, sleeper=waits.append, random_float=lambda: 0.5) == "ok"
    assert waits == [0.5, 1.0]


def test_retry_raises_the_final_error():
    with pytest.raises(RuntimeError, match="permanent"):
        retry_call(lambda: (_ for _ in ()).throw(RuntimeError("permanent")), attempts=2, base_seconds=0, sleeper=lambda _: None)
