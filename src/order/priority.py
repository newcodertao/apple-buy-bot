from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from src.core.models import SKU


def score_sku(sku: SKU, preferences: Any) -> tuple[int, int, int] | None:
    """Lower scores win; every option must be explicitly approved in config."""
    if not sku.available or sku.currency != "CNY" or sku.price > Decimal(preferences.max_price):
        return None
    try:
        return (
            preferences.model_priority.index(sku.model),
            preferences.capacity_priority.index(sku.capacity),
            preferences.color_priority.index(sku.color),
        )
    except ValueError:
        return None


def rank_skus(skus: Iterable[SKU], preferences: Any) -> list[SKU]:
    ranked = [(score, sku) for sku in skus if (score := score_sku(sku, preferences)) is not None]
    # Stable sort preserves the adapter's original order for equivalent choices.
    return [sku for _, sku in sorted(ranked, key=lambda item: item[0])]
