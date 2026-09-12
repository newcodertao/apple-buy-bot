from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from src.core.models import SKU, Platform, SaleMode, StockState


def score_sku(sku: SKU, preferences: Any) -> tuple[int, int, int] | None:
    """Lower scores win; empty variant priorities preserve observed option order."""
    if (
        not sku.capacity.strip()
        or not sku.color.strip()
        or not sku.available
        or sku.currency != "CNY"
        or sku.price > Decimal(preferences.max_price)
    ):
        return None
    if sku.platform != Platform.APPLE and (
        sku.stock_state != StockState.AVAILABLE
        or sku.sale_mode != SaleMode.NORMAL
        or not sku.seller_id
        or not sku.region
        or not sku.platform_item_id
    ):
        return None
    try:
        return (
            preferences.model_priority.index(sku.model),
            preferences.capacity_priority.index(sku.capacity)
            if preferences.capacity_priority else 0,
            preferences.color_priority.index(sku.color) if preferences.color_priority else 0,
        )
    except ValueError:
        return None


def rank_skus(skus: Iterable[SKU], preferences: Any) -> list[SKU]:
    ranked = [(score, sku) for sku in skus if (score := score_sku(sku, preferences)) is not None]
    # Stable sort preserves the adapter's original order for equivalent choices.
    return [sku for _, sku in sorted(ranked, key=lambda item: item[0])]
