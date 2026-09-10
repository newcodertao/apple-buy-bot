"""Parse selected, rendered configuration, never an inferred SKU cross product."""

import re
from decimal import Decimal

from src.core.exceptions import SelectorNotFound
from src.core.models import SKU, Platform
from src.platforms.apple_cn.selectors import NO_APPLECARE_LABEL


def normalized(text: str) -> str:
    return re.sub(r"[\s\u200b-\u200f\u2060\ufeff]+", "", text).casefold()


def money(text: str) -> Decimal:
    match = re.fullmatch(r"\s*(?:RMB|¥|￥)\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*", text)
    if not match or Decimal(match[1].replace(",", "")) <= 0:
        raise SelectorNotFound("UNKNOWN: expected one full CNY price, not an installment")
    return Decimal(match[1].replace(",", ""))


def verify_installment_offer(text: str, expected_total: Decimal) -> None:
    """Accept only the observed 24-term, zero-APR, full-total offer format."""
    match = re.fullmatch(r"24期0%年化利率(rmb[\d,.]+)/月总计(rmb[\d,.]+)", normalized(text))
    if not match or money(match[1].upper()) <= 0 or money(match[2].upper()) != expected_total:
        raise SelectorNotFound("UNKNOWN: 24期0%年化利率或分期总额无法确认")


def parse_skus(data: dict, *, product_id: str = "", model: str = "") -> list[SKU]:
    try:
        selected = {}
        for key in ("colors", "capacities"):
            choices = [x for x in data[key] if x["checked"] and not x["disabled"]]
            if len(choices) != 1:
                raise ValueError
            selected[key] = choices[0]
        capacity = selected["capacities"]["value"].upper()
        color = selected["colors"]["label"].strip()
        models = [x for x in data["models"] if x["checked"] and not x["disabled"]]
        if data["models"] and (
            len(models) != 1 or not normalized(models[0]["label"]).startswith(normalized(model))
        ):
            raise ValueError
        expected = normalized(f"{model} {capacity} {color}")
        if (
            not product_id
            or not model
            or len(data["summary"]) != 1
            or normalized(data["summary"][0]) != expected
        ):
            raise ValueError
        if (
            len(data["price"]) != 1
            or not data["no_trade_in"]
            or data["care"] != [NO_APPLECARE_LABEL]
        ):
            raise ValueError
        price = money(data["price"][0])
        delivery = " / ".join(data["delivery"])
        pickup = " / ".join(dict.fromkeys(data["pickup"]))
        buttons = data["add"] + data["next"]
        if len(buttons) != 1:
            raise ValueError
        unavailable = bool(re.search(r"暂未发售|无货|售罄|暂不供应|无法购买|不可用", delivery))
        enabled = buttons[0]["enabled"]
        if not delivery:
            delivery = (
                "可加入购物袋；配送详情待结账确认" if enabled else "购买按钮不可用；配送详情未加载"
            )
        elif not enabled and not unavailable:
            delivery += "；购买按钮不可用"
        return [
            SKU(
                id="/".join(
                    [
                        normalized(model),
                        selected["capacities"]["value"],
                        selected["colors"]["value"],
                    ]
                ),
                platform=Platform.APPLE,
                product_id=product_id,
                model=model,
                capacity=capacity,
                color=color,
                price=price,
                available=enabled and not unavailable,
                delivery=delivery,
                pickup=pickup or None,
            )
        ]
    except (KeyError, TypeError, ValueError, IndexError):
        raise SelectorNotFound(
            "UNKNOWN: selected Apple configuration or inventory is incomplete"
        ) from None
