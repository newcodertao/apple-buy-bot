"""One locally approved Apple purchase plan; no account credentials or address text."""

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field

from src.core.config import AppConfig
from src.core.exceptions import ConfigurationError, HumanRequired
from src.core.logging import safe_url
from src.core.models import SKU, Model, Platform


class PlannedProduct(Model):
    product_id: str
    url: str
    target_digest: str
    model: str
    capacities: list[str]
    colors: list[str]
    quantity: int = Field(gt=0)
    max_unit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    max_total: Decimal = Field(gt=0, allow_inf_nan=False)


class PurchasePlan(Model):
    products: list[PlannedProduct]
    payment_method: Literal["installments", "wechat"]
    installment_bank: str
    address_basis: Literal[
        "confirm_current_checkout", "selected_saved_address_first_checkout_bind"
    ] = "selected_saved_address_first_checkout_bind"
    market_basis: Literal["manual_cn_confirmation", "apple_cn_direct_configured_product"] = (
        "apple_cn_direct_configured_product"
    )
    currency: Literal["CNY"] = "CNY"
    market: Literal["CN"] = "CN"
    approved_at: datetime | None = None
    address_fingerprint: str = ""
    market_fingerprints: list[str] = Field(default_factory=list)

    def terms(self) -> dict:
        return self.model_dump(
            mode="json", exclude={"approved_at", "address_fingerprint", "market_fingerprints"}
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.terms(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def approved(self):
        return self.model_copy(update={"approved_at": datetime.now(UTC)})

    def require(self, sku: SKU, quantity: int, payment_method: str, bank: str) -> None:
        if self.approved_at is None:
            raise HumanRequired("请先在本机查看并批准本次购买计划，然后继续原任务")
        product = next((p for p in self.products if p.product_id == sku.product_id), None)
        if (
            sku.platform != Platform.APPLE
            or product is None
            or sku.model != product.model
            or not sku.capacity.strip()
            or not sku.color.strip()
            or (product.capacities and sku.capacity not in product.capacities)
            or (product.colors and sku.color not in product.colors)
            or quantity != product.quantity
            or sku.currency != self.currency
            or sku.price > product.max_unit_price
            or sku.price * quantity > product.max_total
            or payment_method != self.payment_method
            or (payment_method == "installments" and bank != self.installment_bank)
        ):
            raise HumanRequired("当前商品、数量、金额或付款条件不符合已批准购买计划")


def draft_plan(config: AppConfig) -> PurchasePlan:
    products = []
    for _, product_id, url in config.targets([Platform.APPLE]):
        prefs = config.preferences_for(product_id)
        products.append(
            PlannedProduct(
                product_id=product_id,
                url=safe_url(url),
                target_digest=hashlib.sha256(url.encode()).hexdigest(),
                model=config.products[product_id].model,
                capacities=prefs.capacity_priority,
                colors=prefs.color_priority,
                quantity=prefs.quantity,
                max_unit_price=prefs.max_price,
                max_total=prefs.max_total or prefs.max_price * prefs.quantity,
            )
        )
    return PurchasePlan(
        products=products,
        payment_method=config.order.payment_method,
        installment_bank=config.order.installment_bank,
    )


def load_plan(path: Path, draft: PurchasePlan) -> PurchasePlan:
    if not path.exists():
        return draft
    try:
        saved = PurchasePlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ConfigurationError("本机购买计划无法读取；请检查本地计划文件，原文件未覆盖") from None
    # Changed configuration requires fresh approval; old approvals never migrate by guess.
    return saved if saved.digest == draft.digest else draft


def save_plan(path: Path, plan: PurchasePlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        raise ConfigurationError("购买计划保存失败；未授权新的购买动作") from None
