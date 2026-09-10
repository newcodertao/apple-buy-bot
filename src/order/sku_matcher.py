from collections.abc import Iterable
from typing import Any

from src.core.models import SKU
from src.order.priority import rank_skus


def best_match(skus: Iterable[SKU], preferences: Any) -> SKU | None:
    ranked = rank_skus(skus, preferences)
    return ranked[0] if ranked else None
