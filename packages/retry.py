"""Retry backoff strategies.

Implements the three curves the assignment names: fixed delay, linear backoff,
and exponential backoff. Kept as a pure function of (strategy, attempt) with no
I/O so the delay maths is directly unit-testable -- see
tests/test_retry_backoff.py.

Lives in `packages/` rather than in the worker because two processes need it:
the worker, when a handler raises, and the reaper, when it recovers a job whose
worker died mid-attempt. Both must produce the same backoff -- a job lost to a
crashed machine should not retry on a different schedule from one that failed
in application code.
"""

from __future__ import annotations

import random

from packages.db.enums import RetryStrategy


def compute_delay_seconds(
    strategy: RetryStrategy,
    attempt: int,
    base_delay_ms: int,
    max_delay_ms: int,
    jitter: bool = True,
) -> float:
    """Seconds to wait before attempt number `attempt + 1`.

    `attempt` is 1-based: the value stored on the job after its first claim.

        fixed        base
        linear       base x attempt
        exponential  base x 2^(attempt-1)

    All three are clamped to `max_delay_ms`. Without a ceiling, exponential
    backoff on a job with max_attempts=20 would schedule its final retry
    roughly six days out.
    """
    attempt = max(attempt, 1)

    if strategy is RetryStrategy.FIXED:
        delay_ms = base_delay_ms
    elif strategy is RetryStrategy.LINEAR:
        delay_ms = base_delay_ms * attempt
    else:  # EXPONENTIAL
        # Cap the exponent before computing the power. 2**attempt with a large
        # attempt is an arbitrarily large integer in Python, and would be
        # computed in full only to be immediately clamped.
        exponent = min(attempt - 1, 32)
        delay_ms = base_delay_ms * (2**exponent)

    delay_ms = min(delay_ms, max_delay_ms)

    if jitter:
        # Full jitter: uniform over [0, delay]. Without it, N jobs that failed
        # together against one downed dependency retry in lockstep and stampede
        # it again the instant it recovers. Randomizing spreads the recovery
        # load across the whole window.
        delay_ms = random.uniform(0, delay_ms)

    return delay_ms / 1000.0
