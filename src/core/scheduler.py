import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

STAGES = (("prepare", -600), ("open_product", -180), ("ready", -30), ("warmup", -5), ("sale", 0))


def backoff(
    attempt: int,
    base: float,
    maximum: float,
    jitter: float = 0.1,
    retry_after: float = 0,
) -> float:
    """Bound exponential retries, while treating server Retry-After as a minimum."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("Attempt must be a non-negative integer")
    if not all(math.isfinite(v) for v in (base, maximum, jitter, retry_after)):
        raise ValueError("Backoff values must be finite")
    if base <= 0 or maximum < 0.3 or not 0 <= jitter <= 1 or retry_after < 0:
        raise ValueError("Invalid backoff bounds")
    capped = attempt >= math.log2(maximum) - math.log2(base)
    delay = maximum if capped else math.ldexp(base, attempt)
    delay = min(maximum, delay * random.uniform(1 - jitter, 1 + jitter))
    # Server rate limits may exceed our normal cap; never retry before their deadline.
    return max(0.3, retry_after, delay)


class Scheduler:
    stages = STAGES

    def __init__(
        self,
        sale_time: datetime | None,
        stop_event: asyncio.Event,
        *,
        now: Callable[[], datetime] | None = None,
        waiter: Callable[[asyncio.Event, float], Awaitable[bool]] | None = None,
    ):
        if sale_time is not None and (sale_time.tzinfo is None or sale_time.utcoffset() is None):
            raise ValueError("Sale time must include a timezone")
        self.sale_time = sale_time
        self.stop_event = stop_event
        self._now = now or (lambda: datetime.now(UTC))
        self._waiter = waiter

    async def wait_until(self, offset_seconds: float = 0) -> bool:
        if not math.isfinite(offset_seconds):
            raise ValueError("Offset must be finite")
        if self.sale_time is None:
            return not self.stop_event.is_set()
        target = self.sale_time + timedelta(seconds=offset_seconds)
        while not self.stop_event.is_set():
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return True
            if self._waiter is not None:
                if not await self._waiter(self.stop_event, min(remaining, 1.0)):
                    return False
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=min(remaining, 1.0))
            except TimeoutError:
                continue
        return False
