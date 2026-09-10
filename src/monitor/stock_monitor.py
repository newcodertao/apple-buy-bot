import asyncio
import random
from datetime import datetime
from typing import Any


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Return False on stop; no polling loop or uninterruptible sleep."""
    if stop.is_set():
        return False
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0, seconds))
        return False
    except TimeoutError:
        return not stop.is_set()


def polling_interval(settings: Any, sale_time: datetime | None) -> float:
    if sale_time is None:
        base = settings.normal_interval
    else:
        remaining = (sale_time - datetime.now(sale_time.tzinfo)).total_seconds()
        if -settings.sale_window_seconds <= remaining <= 5:
            base = settings.sale_interval
        elif 5 < remaining <= 30:
            base = settings.warmup_interval
        else:
            base = settings.normal_interval
    # Jitter only adds time so the configured minimum request interval is respected.
    return base + random.uniform(0, settings.jitter)
