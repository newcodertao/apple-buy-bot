"""Ordinary marketplace page flow with strict visible evidence at each boundary.

The small per-platform selector maps contain only inspected UI structures.
Missing fields are human handoff points, never invented product or stock data.
"""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Locator, Page

from src.browser.manager import BrowserManager
from src.core.config import OrderSettings, ProductPreferences, ProductTarget
from src.core.exceptions import HumanRequired, SelectorNotFound
from src.core.models import (
    SKU,
    FinancingOffer,
    LoginStatus,
    OrderResult,
    OrderReview,
    Platform,
    SaleMode,
    StockState,
    Verification,
)
from src.order.checkout import financing_state, verify_checkout
from src.platforms.inspection import InspectionAdapter


def normalize(value: str) -> str:
    return re.sub(r"[\s\u200b-\u200f\ufeff]+", "", value).casefold()


def model_in_title(model: str, title: str) -> bool:
    """A model may be followed by capacity, but never a longer model suffix."""
    return bool(
        re.search(re.escape(normalize(model)) + r"(?=$|[^a-z0-9]|\d+(?:gb|tb))", normalize(title))
    )


def visible_stock_state(text: str) -> StockState:
    if re.search(r"无货|暂无|无现货|缺货|售罄|库存不足|预售|定金|尾款|不支持|无法|暂停", text):
        return StockState.UNAVAILABLE
    if re.search(r"有货|现货|预计.{0,40}送达", text):
        return StockState.AVAILABLE
    return StockState.UNKNOWN


def cny(value: str, *, zero: bool = False) -> Decimal:
    """One full amount only; monthly, range, crossed-out and promotional text fail."""
    match = re.fullmatch(
        r"\s*(?:[¥￥]|RMB|CNY)?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:元)?\s*",
        value,
        re.I,
    )
    if not match:
        raise SelectorNotFound("无法确认页面上的完整人民币金额")
    amount = Decimal(match[1].replace(",", ""))
    if amount < 0 or (not zero and amount == 0):
        raise SelectorNotFound("页面金额无效")
    return amount


def platform_item_id(url: str, platform: Platform) -> str:
    parsed = urlsplit(url)
    if platform == Platform.JD:
        match = re.fullmatch(r"/(\d+)\.html", parsed.path)
        return match[1] if match else ""
    values = parse_qs(parsed.query).get("id", [])
    return values[0] if len(values) == 1 and values[0].isdigit() else ""


def matching_option(text: str, candidates: list[str]) -> str:
    """Match a complete rendered option, never a shorter model inside another."""
    values = [candidate for candidate in candidates if normalize(candidate) == normalize(text)]
    if len(values) != 1:
        raise SelectorNotFound("商品所选规格与配置无法唯一对应")
    return values[0]


def parse_product(
    data: dict,
    *,
    platform: Platform,
    product_id: str,
    preferences: ProductPreferences,
) -> SKU:
    """Pure parser for visible product snapshots; target policy is checked later."""
    try:
        item_id = data["item_id"]
        if not item_id or not data["title"]:
            raise ValueError
        model = matching_option(data["model"], preferences.model_priority)
        capacity = matching_option(data["capacity"], preferences.capacity_priority)
        color = matching_option(data["color"], preferences.color_priority)
        if not model_in_title(model, data["title"]):
            raise ValueError
        stock = StockState(data.get("stock_state", "UNKNOWN"))
        sale = SaleMode(data.get("sale_mode", "UNKNOWN"))
        # Availability requires a complete current-region stock statement.
        available = (
            stock == StockState.AVAILABLE
            and sale == SaleMode.NORMAL
            and bool(data.get("region"))
            and bool(data.get("seller_id"))
            and data.get("buy_enabled") is True
        )
        return SKU(
            id="/".join([item_id, normalize(model), normalize(capacity), normalize(color)]),
            platform=platform,
            product_id=product_id,
            platform_item_id=item_id,
            model=model,
            capacity=capacity,
            color=color,
            price=cny(data["price"]),
            available=available,
            delivery=data.get("stock_text") or None,
            seller_id=data.get("seller_id", ""),
            shop_name=data.get("shop_name", ""),
            region=data.get("region", ""),
            observed_at=datetime.now(UTC),
            stock_state=stock,
            sale_mode=sale,
        )
    except (KeyError, TypeError, ValueError):
        raise SelectorNotFound("商品身份、所选规格或完整报价尚未加载") from None


class MarketplaceAdapter(InspectionAdapter):
    """Shared operation flow; platform maps describe the observed DOM only."""

    selectors: dict[str, str] = {}
    login_hosts: tuple[str, ...] = ()
    navigation_hosts: dict[str, tuple[str, ...]] = {}
    cart_url = ""
    phase = "marketplace"
    public_product_access = True

    def __init__(
        self,
        manager: BrowserManager,
        screenshots_dir: Path,
        preferences: dict[str, ProductPreferences] | None = None,
        *,
        targets: dict[str, ProductTarget] | None = None,
        order_policy: OrderSettings | None = None,
        allow_submit: bool = False,
    ):
        super().__init__(manager, screenshots_dir)
        self.preferences = preferences or {}
        self.targets = targets or {}
        self.order_policy = order_policy or OrderSettings()
        self._allow_submit = allow_submit
        self.selected: SKU | None = None
        self._quantity = 0
        self._cart_attempted = False
        self._submit_attempted = False
        self._last_review: OrderReview | None = None

    def _page(self) -> Page:
        page = self.manager.current_page(self.platform)
        if page is None:
            raise SelectorNotFound("UNKNOWN: 请先打开目标商品页面")
        self._validate_platform_url(page.url)
        return page

    def _field(self, key: str, scope: Page | Locator | None = None) -> Locator:
        selector = self.selectors.get(key)
        if not selector:
            raise SelectorNotFound(f"当前平台的 {key} 页面结构尚待核验，请人工处理此步")
        return (scope or self._page()).locator(selector)

    async def _text(self, key: str, scope: Page | Locator | None = None) -> str:
        visible = self._field(key, scope).filter(visible=True)
        if await visible.count() != 1:
            raise SelectorNotFound(f"无法唯一读取当前页面的 {key}")
        return (await visible.inner_text()).strip()

    async def _maybe_text(self, key: str, scope: Page | Locator | None = None) -> str:
        if key not in self.selectors:
            return ""
        visible = self._field(key, scope).filter(visible=True)
        return (await visible.inner_text()).strip() if await visible.count() == 1 else ""

    def restore_review(self, review: OrderReview) -> None:
        """Restore only the expected receipt identity; never rearm a transaction."""
        if review.platform != self.platform:
            raise HumanRequired("恢复的订单摘要与当前渠道不符")
        self._last_review = review
        self._submit_attempted = True
        self._cart_attempted = True

    async def _click(self, key: str) -> None:
        await self._guard_human()
        action = self._field(key).filter(visible=True)
        if await action.count() != 1 or not await action.is_enabled():
            raise SelectorNotFound("当前操作入口不可用或存在多个，请人工核对")
        page = self._page()
        hosts = self.navigation_hosts.get(key, (urlsplit(page.url).hostname, *self.login_hosts))
        async with self.navigation_guard(page, allowed_hosts=hosts):
            await action.click()

    async def _choose(self, key: str, wanted: str) -> None:
        options = self._field(key).filter(visible=True)
        choices = []
        for option in await options.all():
            label = (await option.inner_text()).strip()
            if normalize(label) == normalize(wanted):
                choices.append(option)
        if len(choices) != 1 or not await choices[0].is_enabled():
            raise SelectorNotFound("当前页面没有唯一可用的目标规格")
        await self._guard_human()
        page = self._page()
        hosts = self.navigation_hosts.get(key, (urlsplit(page.url).hostname, *self.login_hosts))
        async with self.navigation_guard(page, allowed_hosts=hosts):
            await choices[0].click()

    async def product_snapshot(self) -> dict:
        """Default DOM reader for current visible selection and price."""
        return {
            "item_id": platform_item_id(self._page().url, self.platform),
            "title": await self._text("product_name"),
            "price": await self._text("product_price"),
            "model": await self._text("selected_model"),
            "capacity": await self._text("selected_capacity"),
            "color": await self._text("selected_color"),
            "seller_id": await self._maybe_text("seller_id"),
            "shop_name": await self._maybe_text("shop_name"),
            "region": await self._maybe_text("region"),
            "stock_text": await self._maybe_text("stock"),
            "stock_state": "UNKNOWN",
            "sale_mode": "UNKNOWN",
            "buy_enabled": await self._field("add_to_cart").is_enabled(),
        }

    def _target_check(self, sku: SKU) -> None:
        target = self.targets.get(self.product_id)
        if (
            target is None
            or not target.seller_ids
            or sku.seller_id not in target.seller_ids
            or not target.region
            or normalize(sku.region) != normalize(target.region)
        ):
            raise HumanRequired("商品销售方或配送地区与白名单不符，请在配置中核对")
        if not sku.available or sku.stock_state != StockState.AVAILABLE:
            raise HumanRequired("目标地区库存或购买条件尚未确认")

    async def get_skus(self) -> list[SKU]:
        async with self._lock:
            await self._guard_human()
            preferences = self.preferences.get(self.product_id)
            if preferences is None:
                raise SelectorNotFound("UNKNOWN: 请先配置目标商品、型号和规格")
            # Inspect the selected variant first. Changing to a lower-priority
            # option is allowed only through actually mapped choice controls.
            for key, values in (
                ("model_options", preferences.model_priority),
                ("capacity_options", preferences.capacity_priority),
                ("color_options", preferences.color_priority),
            ):
                if key not in self.selectors:
                    continue
                options = self._field(key).filter(visible=True)
                labels = [normalize(await x.inner_text()) for x in await options.all()]
                wanted = next((value for value in values if normalize(value) in labels), None)
                if wanted:
                    await self._choose(key, wanted)
            previous = None
            for _ in range(12):
                data = await self.product_snapshot()
                if data == previous:
                    return [
                        parse_product(
                            data,
                            platform=self.platform,
                            product_id=self.product_id,
                            preferences=preferences,
                        )
                    ]
                previous = data
                await asyncio.sleep(0.25)
            raise SelectorNotFound("商品规格或报价尚在变化，请稍后核对")

    async def check_stock(self) -> list[SKU]:
        return await self.get_skus()

    async def select_sku(self, sku: SKU) -> None:
        async with self._lock:
            await self._guard_human()
            if sku.platform != self.platform or sku.product_id != self.product_id:
                raise HumanRequired("所选规格不属于当前渠道商品")
            current = parse_product(
                await self.product_snapshot(),
                platform=self.platform,
                product_id=self.product_id,
                preferences=self.preferences[self.product_id],
            )
            if current.id != sku.id or current.price != sku.price:
                raise HumanRequired("当前商品规格或报价已变化，请重新监测")
            self._target_check(current)
            self.selected = current

    async def _cart_check(self, quantity: int) -> None:
        sku = self.selected
        if sku is None:
            raise HumanRequired("请先核验目标规格")
        rows = self._field("cart_items").filter(visible=True)
        if await rows.count() != 1:
            raise HumanRequired("购物车须仅包含目标商品，请人工整理已有内容")
        row = rows.first
        text = normalize(await self._text("cart_name", row))
        if not model_in_title(sku.model, text):
            raise HumanRequired("购物车商品型号与目标不符")
        for expected in (sku.capacity, sku.color):
            if normalize(expected) not in text:
                raise HumanRequired("购物车商品规格与目标不符")
        if (
            await self._text("cart_seller_id", row) != sku.seller_id
            or await self._text("cart_item_id", row) != sku.platform_item_id
            or await self._field("cart_quantity", row).input_value() != str(quantity)
            or not await self._field("cart_selected", row).is_checked()
            or cny(await self._text("cart_price", row)) != sku.price
            or cny(await self._text("cart_total")) != sku.price * quantity
        ):
            raise HumanRequired("购物车销售方、数量、勾选状态或金额变化，请人工核对")

    async def add_to_cart(self, quantity: int) -> None:
        async with self._lock:
            await self._guard_human()
            if self.selected is None:
                raise HumanRequired("请先选择并核验商品")
            self._target_check(self.selected)
            self._quantity = quantity
            if "cart_items" in self.selectors and await self._field("cart_items").count():
                await self._cart_check(quantity)
                return
            if self._cart_attempted:
                raise HumanRequired("已经尝试加购，请先核对购物车，禁止重复加购")
            # Checking an empty cart is a separate observed UI proof; no cleanup.
            if "cart_empty" not in self.selectors or "open_cart" not in self.selectors:
                raise SelectorNotFound("购物车初始内容尚未核验，请人工进入购物车")
            await self._click("open_cart")
            if not await self._field("cart_empty").is_visible():
                raise HumanRequired("购物车已有商品或状态未知，请人工核对")
            await self._navigate(self._page(), self.targets[self.product_id].url)
            # A cart check may change the selected variant, so verify again.
            current = parse_product(
                await self.product_snapshot(),
                platform=self.platform,
                product_id=self.product_id,
                preferences=self.preferences[self.product_id],
            )
            if current.id != self.selected.id or current.price != self.selected.price:
                raise HumanRequired("检查购物车后商品规格变化，请重新选择")
            self._target_check(current)
            if quantity != 1:
                field = self._field("product_quantity")
                await field.fill(str(quantity))
                await field.press("Tab")
                if await field.input_value() != str(quantity):
                    raise HumanRequired("当前页面不允许目标数量")
            self._cart_attempted = True
            await self._click("add_to_cart")
            await self._field("cart_added").wait_for(state="visible", timeout=10000)
            await self._click("view_cart")
            await self._cart_check(quantity)

    async def goto_checkout(self) -> None:
        async with self._lock:
            await self._guard_human()
            if "submit_order" in self.selectors and await self._field("submit_order").is_visible():
                return
            await self._cart_check(self._quantity)
            await self._click("checkout")
            await self._field("submit_order").wait_for(state="visible", timeout=10000)
            await self._guard_human()

    async def _read_financing(self, total: Decimal, *, stage: str = "PRE_ORDER"):
        # A marketing banner never supplies an offer. All these mapped fields
        # belong to one selected financing plan in checkout/cashier UI.
        if "financing_selected" not in self.selectors:
            return None
        selected = self._field("financing_selected").filter(visible=True)
        if await selected.count() != 1:
            return None
        terms = await self._text("financing_terms", selected)
        match = re.fullmatch(r"(\d+)\s*期", terms)
        if not match:
            return None
        return FinancingOffer(
            provider=await self._text("financing_provider", selected),
            terms=int(match[1]),
            interest=cny(await self._text("financing_interest", selected), zero=True),
            service_fee=cny(await self._text("financing_fee", selected), zero=True),
            total_repayment=cny(await self._text("financing_total", selected)),
            principal=cny(await self._text("financing_principal", selected)),
            selected=True,
            state="ELIGIBLE",
            stage=stage,
            verified_at=datetime.now(UTC),
        )

    async def _read_review(self) -> OrderReview:
        sku = self.selected
        if sku is None:
            raise HumanRequired("缺少已核验商品，请重新开始演练")
        rows = self._field("review_items").filter(visible=True)
        if await rows.count() != 1:
            raise HumanRequired("结算商品不止一行，请人工核对赠品或附加商品")
        row = rows.first
        name = normalize(await self._text("review_name", row))
        if not model_in_title(sku.model, name) or any(
            normalize(v) not in name for v in (sku.capacity, sku.color)
        ):
            raise HumanRequired("结算商品规格不符")
        raw_quantity = await self._text("review_quantity", row)
        quantity = re.fullmatch(r"(?:数量[:：]?\s*|[x×]\s*)?(\d+)", raw_quantity)
        if not quantity:
            raise SelectorNotFound("结算数量无法确认")
        total = cny(await self._text("review_total"))
        review = OrderReview(
            platform=self.platform,
            product_id=sku.product_id,
            sku_id=sku.id,
            model=sku.model,
            capacity=sku.capacity,
            color=sku.color,
            platform_item_id=await self._text("review_item_id", row),
            seller_id=await self._text("review_seller_id", row),
            shop_name=await self._text("review_shop", row),
            region=await self._text("review_region"),
            observed_at=datetime.now(UTC),
            stock_state=visible_stock_state(await self._maybe_text("review_stock")),
            sale_mode=sku.sale_mode,
            unit_price=cny(await self._text("review_unit_price", row)),
            quantity=int(quantity[1]),
            total_price=total,
            items_subtotal=cny(await self._text("review_subtotal")),
            discount=cny(await self._text("review_discount"), zero=True),
            shipping=cny(await self._text("review_shipping"), zero=True),
            fees=cny(await self._text("review_fees"), zero=True),
            address_present=await self._field("review_address_selected").is_checked(),
            checkout_valid=await self._field("submit_order").is_enabled(),
            line_items=1,
            financing=await self._read_financing(total),
        )
        verify_checkout(
            review,
            sku,
            self.preferences[self.product_id],
            target=self.targets.get(self.product_id),
            order_policy=self.order_policy,
        )
        return review

    async def verify_order(self) -> OrderReview:
        async with self._lock:
            await self._guard_human()
            self._last_review = await self._read_review()
            return self._last_review

    async def login_status(self) -> LoginStatus:
        async with self._lock:
            verification = await self._detect_verification()
            if verification.reason == "login":
                return LoginStatus.REQUIRED
            if verification.required or self.manager.current_page(self.platform) is None:
                return LoginStatus.UNKNOWN
            if "authenticated" in self.selectors:
                if await self._field("authenticated").filter(visible=True).count() == 1:
                    return LoginStatus.AUTHENTICATED
            return LoginStatus.UNKNOWN

    async def _detect_verification(self) -> Verification:
        page = self.manager.current_page(self.platform)
        if page is not None and (urlsplit(page.url).hostname or "") in self.login_hosts:
            return Verification(required=True, reason="login")
        return await super()._detect_verification()

    async def _read_receipt(self, expected_id: str | None = None) -> OrderResult:
        review = self._last_review
        if review is None:
            return OrderResult(status="UNKNOWN", message="缺少提交前摘要，当前页面需人工核对")
        try:
            order_id = await self._text("receipt_order_id")
            if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", order_id):
                raise ValueError
            if expected_id is not None and order_id != expected_id:
                raise ValueError
            if await self._text("receipt_status") not in {"等待付款", "待付款", "等待买家付款"}:
                raise ValueError
            if (
                await self._text("receipt_item_id") != review.platform_item_id
                or await self._text("receipt_seller_id") != review.seller_id
                or await self._text("receipt_quantity") != str(review.quantity)
                or cny(await self._text("receipt_total")) != review.total_price
            ):
                raise ValueError
            offer = await self._read_financing(review.total_price, stage="POST_ORDER")
            state = financing_state(offer, review.total_price)
            return OrderResult(
                status="SUCCESS",
                order_id=order_id,
                payment_state="UNPAID",
                financing_state=state.value,
                message="待付款订单已核对，分期及最终付款请在原订单页面处理",
            )
        except (HumanRequired, ValueError):
            return OrderResult(status="UNKNOWN", message="未取得完整待付款回执，请核对原订单")

    async def read_order_status(self, order_id: str | None = None) -> OrderResult:
        async with self._lock:
            await self._guard_human()
            return await self._read_receipt(order_id)

    async def submit_order(self) -> OrderResult:
        async with self._lock:
            self._page()
            if not self._allow_submit:
                raise HumanRequired("最终提交关闭，请核对本地演练与提交开关")
            if self._submit_attempted:
                return OrderResult(status="UNKNOWN", message="已经尝试提交，禁止重复点击")
            await self._guard_human()
            self._last_review = await self._read_review()
            # No payment or credit-opening controls are part of this adapter.
            self._submit_attempted = True
            try:
                await self._click("submit_order")
                await self._field("receipt_order_id").wait_for(state="visible", timeout=15000)
                return await self._read_receipt()
            except Exception:
                return OrderResult(status="UNKNOWN", message="提交结果待核对，保留订单保护")
