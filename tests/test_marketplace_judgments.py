"""Focused regressions for the three final marketplace decision fixes."""

import pytest

from src.core.config import ProductPreferences
from src.core.exceptions import SelectorNotFound
from src.core.models import Platform, StockState
from src.platforms.jd.adapter import has_installment_interest
from src.platforms.marketplace import model_in_title, parse_product, visible_stock_state


def test_model_boundary_rejects_larger_model_and_accepts_attached_capacity():
    for model, title, expected in (
        ("iPhone 17 Pro", "Apple iPhone 17 Pro Max 512GB 黑色", False),
        ("iPhone 17", "Apple iPhone 17 Pro 512GB 黑色", False),
        ("iPhone 17", "Apple iPhone 17 512GB 黑色", True),
        ("iPhone 17 Pro", "Apple iPhone 17 Pro512GB 黑色", True),
        ("iPhone 17 Pro Max", "Apple iPhone 17 Pro Max1TB 黑色", True),
    ):
        assert model_in_title(model, title) is expected
        preferences = ProductPreferences(
            model_priority=[model], capacity_priority=["512GB"], color_priority=["黑色"]
        )
        data = {
            "item_id": "123",
            "title": title,
            "model": model,
            "capacity": "512GB",
            "color": "黑色",
            "price": "100",
        }
        if expected:
            assert (
                parse_product(
                    data, platform=Platform.TMALL, product_id="fixture", preferences=preferences
                ).model
                == model
            )
        else:
            with pytest.raises(SelectorNotFound):
                parse_product(
                    data, platform=Platform.TMALL, product_id="fixture", preferences=preferences
                )


def test_negative_stock_takes_priority_over_positive_words_and_delivery():
    for message in (
        "暂无现货",
        "无现货",
        "现货售罄",
        "当前缺货",
        "有货但库存不足",
        "预售现货",
        "支付定金，预计明天送达",
    ):
        assert visible_stock_state(message) == StockState.UNAVAILABLE
    assert visible_stock_state("有货，预计明天送达") == StockState.AVAILABLE
    assert visible_stock_state("配送情况待确认") == StockState.UNKNOWN


def test_fractional_installment_interest_is_not_free():
    for fee in ("含利息￥0.50", "含利息 ¥ 0.01", "每期含利息￥1,000.00"):
        assert has_installment_interest(fee)
    assert not has_installment_interest("含利息￥0.00")
    assert not has_installment_interest("费用尚未显示")
