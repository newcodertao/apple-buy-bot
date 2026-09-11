"""Parse selected, rendered configuration, never an inferred SKU cross product."""

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class InstallmentTerms:
    bank: str
    principal: Decimal
    total: Decimal
    interest: Decimal
    fee: Decimal
    payments: tuple[Decimal, ...]


def verify_installment_offer(
    text: str, expected_total: Decimal, *, bank: str = ""
) -> InstallmentTerms:
    """Require one complete selected-plan disclosure, not its APR advertisement.

    A rounded `约 RMB 284/月` is display text. It cannot replace the actual
    repayment schedule: all 24 amounts, or the first 23 plus the final amount,
    must be shown and add up exactly at cent precision.
    """
    value = normalized(text)
    amount = r"(?:rmb|¥|￥)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?![\d.,])"

    def one(pattern: str) -> Decimal:
        found = re.findall(pattern, value)
        if len(found) != 1:
            raise ValueError
        return Decimal(found[0].replace(",", ""))

    try:
        if not bank or normalized(bank) not in value:
            raise ValueError
        if len(re.findall(r"(?<!第)(?<!\d)24(?:期|个月)", value)) != 1:
            raise ValueError
        if any(Decimal(rate) != 0 for rate in re.findall(r"([\d.]+)%年化利率", value)):
            raise ValueError
        principal = one(r"本金[:：]?" + amount)
        total = one(r"(?:总还款额|总计)[:：]?" + amount)
        interest = one(r"(?:总利息|利息)[:：]?" + amount)
        fee = one(r"(?:总手续费|手续费|服务费)[:：]?" + amount)
        if principal != expected_total or total != expected_total or interest != 0 or fee != 0:
            raise ValueError
        # Accept a complete per-period disclosure or an explicit final-period
        # adjustment. Neither format is inferred from the monthly banner.
        per_period = re.findall(r"第(\d+)期[:：]?" + amount, value)
        if per_period:
            if len(per_period) != 24 or {int(i) for i, _ in per_period} != set(range(1, 25)):
                raise ValueError
            payments = tuple(
                Decimal(payment.replace(",", ""))
                for _, payment in sorted(per_period, key=lambda pair: int(pair[0]))
            )
        else:
            regular = one(r"前23期每期[:：]?" + amount)
            final = one(r"末期[:：]?" + amount)
            payments = (regular,) * 23 + (final,)
        if any(payment <= 0 for payment in payments) or sum(payments) != total:
            raise ValueError
        monthly = re.findall(r"(约)?" + amount + r"/月", value)
        for approximate, displayed in monthly:
            shown = Decimal(displayed.replace(",", ""))
            if (not approximate and shown != payments[0]) or (
                approximate and abs(shown - payments[0]) >= 1
            ):
                raise ValueError
        return InstallmentTerms(bank, principal, total, interest, fee, payments)
    except (ValueError, ArithmeticError):
        raise SelectorNotFound(
            "UNKNOWN: 24期分期的银行、本金、利息、手续费或完整还款计划无法核验"
        ) from None


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
        # The new-product Continue branch has no verified purchase transition.
        # Even if it becomes enabled it does not prove bag eligibility.
        enabled = buttons[0]["enabled"] and bool(data["add"])
        if data["next"] and data["next"][0]["enabled"]:
            raise SelectorNotFound("NOT RUN: Apple Continue 分支尚未实页验证")
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
