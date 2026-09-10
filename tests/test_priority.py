from decimal import Decimal

import pytest

from src.core.config import ProductPreferences
from src.core.models import SKU, Platform
from src.order.priority import rank_skus, score_sku
from src.order.sku_matcher import best_match


def sku(identifier: str = "s1", **changes) -> SKU:
    fields = dict(
        id=identifier, platform=Platform.APPLE, product_id="iphone",
        model="iPhone 18 Pro Max", capacity="512GB", color="黑色",
        price=Decimal("14999.99"), available=True,
    )
    fields.update(changes)
    return SKU(**fields)


def test_lexicographic_model_capacity_color_priority():
    preferences = ProductPreferences()
    options = [sku("pro", model="iPhone 18 Pro"), sku("256", capacity="256GB"),
               sku("silver", color="银色"), sku("black")]
    assert [choice.id for choice in rank_skus(options, preferences)] == [
        "black", "silver", "256", "pro",
    ]
    assert best_match(options, preferences).id == "black"


@pytest.mark.parametrize("changes", [
    {"available": False}, {"price": Decimal("15000.01")}, {"currency": "USD"},
    {"model": "Unknown model"}, {"capacity": "128GB"}, {"color": "unknown"},
])
def test_unapproved_or_unavailable_choices_excluded(changes):
    candidate = sku(**changes)
    preferences = ProductPreferences()
    assert score_sku(candidate, preferences) is None
    assert best_match([candidate], preferences) is None


def test_price_limit_equality_and_stable_ties():
    preferences = ProductPreferences()
    first = sku("first", price=Decimal("15000"))
    second = sku("second")
    assert score_sku(first, preferences) == (0, 0, 0)
    assert rank_skus([first, second], preferences) == [first, second]
