import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class RateLimiter:
    """Sliding-window limit of `max_calls` per `period` seconds, per key.

    In-memory, so it's per process: enough to blunt abuse of the one or two
    expensive endpoints on a single small Fly machine, not a distributed quota.
    """

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        """Raise 429 if `key` is at its limit, else count this call."""
        self.ensure_room(key)
        self.record(key)

    def ensure_room(self, key: str) -> None:
        """Raise 429 if `key` is at its limit, without counting anything.

        With `record`, this limits only some calls, e.g. failed attempts.
        """
        now = time.monotonic()
        calls = self._calls[key]
        while calls and now - calls[0] > self.period:
            calls.popleft()
        if len(calls) >= self.max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please wait a moment and try again.",
                headers={"Retry-After": str(int(self.period))},
            )

    def record(self, key: str) -> None:
        self._calls[key].append(time.monotonic())
