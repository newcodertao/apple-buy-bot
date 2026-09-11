import asyncio
import re
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import expect

from src.core.config import ProductPreferences
from src.core.exceptions import ConfigurationError, HumanRequired, SelectorNotFound
from src.core.models import (
    SKU,
    LoginStatus,
    OrderResult,
    OrderReview,
    Platform,
    State,
    Verification,
)
from src.order.checkout import verify_checkout
from src.platforms.apple_cn.parser import money, normalized, parse_skus, verify_installment_offer
from src.platforms.apple_cn.selectors import (
    ADDRESS_FIELDS,
    NO_APPLECARE_LABEL,
    PRODUCT_SNAPSHOT,
    SELECTORS,
)
from src.platforms.inspection import InspectionAdapter


class AppleCNAdapter(InspectionAdapter):
    """Current China purchase flow, with strict review and single-attempt submission."""

    platform = Platform.APPLE
    # Apple permits public configuration/bag building. Login is enforced when
    # the actual sign-in page appears and before reviewing/submitting an order.
    public_states = frozenset(
        {
            State.PREPARING,
            State.WAITING,
            State.MONITORING,
            State.SELECTING_SKU,
            State.ADDING_CART,
            State.CHECKOUT,
        }
    )

    def __init__(
        self,
        manager,
        screenshots_dir: Path,
        preferences=None,
        *,
        allow_submit=False,
        payment_method="installments",
        installment_bank="中国建设银行",
    ):
        super().__init__(manager, screenshots_dir)
        self.preferences: dict[str, ProductPreferences] = preferences or {}
        self.selected: SKU | None = None
        self._cart_attempted = False
        self._quantity = 1
        self._allow_submit = allow_submit
        self._submit_attempted = False
        self._last_order_id: str | None = None
        self.payment_method = payment_method
        self.installment_bank = installment_bank
        self._installment_verified_at = None

    def _page(self):
        page = self.manager.current_page(self.platform)
        if page is None:
            raise SelectorNotFound("UNKNOWN: open a configured Apple product first")
        self._validate_platform_url(page.url)
        return page

    def _locator(self, key):
        selector = SELECTORS[key]
        if selector is None:
            raise SelectorNotFound("UNKNOWN: Apple " + key + " needs real-page verification")
        return self._page().locator(selector)

    async def _snapshot(self):
        await self._guard_human()
        return await self._page().evaluate(PRODUCT_SNAPSHOT, SELECTORS)

    async def _choose(self, key, wanted):
        controls = self._locator(key)
        matches = []
        for control in await controls.all():
            info = await control.evaluate(
                "e=>({value:e.value,label:Array.from(e.labels||[]).map(x=>x.innerText).join(' ')})"
            )
            label = info["label"].strip().split("\n")[0]
            if normalized(info["value"]) == normalized(wanted) or normalized(label) == normalized(
                wanted
            ):
                matches.append(control)
        if len(matches) != 1:
            raise SelectorNotFound("UNKNOWN: requested Apple " + key + " is missing or ambiguous")
        await self._guard_human()
        control = matches[0]
        await expect(control).to_be_enabled()
        if not await control.is_checked():
            # Apple's styled labels and sticky bar can cover the native radio.
            # Use its normal keyboard interaction, never force a pointer click.
            await control.press("Space")
            await expect(control).to_be_checked()

    async def _configure(self, model, capacity, color) -> SKU:
        if await self._locator("model").count():
            await self._choose("model", model)
        await self._choose("color", color)
        await self._choose("capacity", capacity)
        await self._choose("no_trade_in", "noTradeIn")
        await self._choose("applecare", NO_APPLECARE_LABEL)
        # React updates selected controls, price, and fulfillment separately.
        # Require matching complete snapshots twice before reporting inventory.
        previous = None
        for _ in range(32):
            snapshot = await self._snapshot()
            try:
                sku = parse_skus(snapshot, product_id=self.product_id, model=model)[0]
                if normalized(sku.capacity) != normalized(capacity) or sku.color != color:
                    raise SelectorNotFound("UNKNOWN: configuration is still changing")
                if snapshot == previous:
                    return sku
                previous = snapshot
            except SelectorNotFound:
                previous = None
            await asyncio.sleep(0.25)
        raise SelectorNotFound("UNKNOWN: Apple configuration did not settle")

    async def get_skus(self) -> list[SKU]:
        async with self._lock:
            try:
                await self._guard_human()
                self._page()
                prefs = self.preferences.get(self.product_id)
                if prefs is None:
                    raise SelectorNotFound(
                        "UNKNOWN: configure product preferences before SKU discovery"
                    )
                if (
                    len(prefs.model_priority)
                    * len(prefs.capacity_priority)
                    * len(prefs.color_priority)
                    > 64
                ):
                    raise ConfigurationError(
                        "Apple candidate search is limited to 64 configurations"
                    )
                await self._locator("color").first.wait_for(state="attached")
                skus = []
                for model in prefs.model_priority:
                    for capacity in prefs.capacity_priority:
                        for color in prefs.color_priority:
                            sku = await self._configure(model, capacity, color)
                            skus.append(sku)
                            if sku.available and sku.price <= prefs.max_price:
                                return skus
                return skus
            except (SelectorNotFound, PlaywrightTimeout):
                await self._capture("sku_unknown", "WAITING_HUMAN")
                raise SelectorNotFound(
                    "UNKNOWN: Apple SKU controls changed or are incomplete"
                ) from None

    async def check_stock(self) -> list[SKU]:
        return await self.get_skus()

    async def select_sku(self, sku: SKU) -> None:
        async with self._lock:
            if sku.platform != self.platform or sku.product_id != self.product_id:
                raise HumanRequired("Selected SKU belongs to a different product")
            actual = await self._configure(sku.model, sku.capacity, sku.color)
            if actual.id != sku.id or actual.price != sku.price or not actual.available:
                raise HumanRequired("Selected SKU price or availability changed")
            self.selected = actual

    async def _bag_check(self, quantity: int):
        sku = self.selected
        if sku is None:
            raise HumanRequired("No verified SKU selected")
        await self._locator("bag").wait_for(state="visible")
        items = self._locator("bag_items")
        if await items.count() != 1:
            raise HumanRequired("购物袋必须仅有目标商品一行，请人工核对")
        item = items.first
        name = await item.locator(SELECTORS["bag_name"]).inner_text()
        count = await item.locator(SELECTORS["bag_quantity"]).input_value()
        price = money(await item.locator(SELECTORS["bag_price"]).inner_text())
        total = money(await self._locator("bag_total").inner_text())
        error = await self._locator("bag_error").inner_text()
        if (
            normalized(name) != normalized(f"{sku.model} {sku.capacity} {sku.color}")
            or count != str(quantity)
            or price != sku.price * quantity
            or total != sku.price * quantity
            or error.strip()
        ):
            raise HumanRequired("购物袋商品、数量、金额或库存异常，请人工核对")

    async def add_to_cart(self, quantity: int) -> None:
        async with self._lock:
            await self._guard_human()
            if self.selected is None:
                raise HumanRequired("Select and verify a SKU before adding to bag")
            if quantity not in (1, 2):
                raise HumanRequired("Apple 已验证流程只支持页面允许的 1 至 2 件")
            self._quantity = quantity
            if await self._locator("bag").is_visible():
                await self._bag_check(quantity)
                return
            if self._cart_attempted:
                raise HumanRequired("已尝试加购，请人工核对购物袋；禁止重复点击")
            # An enabled button alone is insufficient when the site's delivery
            # component failed (observed HTTP 541 in the isolated live profile).
            # Leave the browser for ordinary manual recovery, without an add.
            if not (await self._snapshot()).get("delivery"):
                raise HumanRequired("官网配送信息未加载，请在程序浏览器检查并人工确认购物袋")
            # Inspect the real bag menu before any add; existing bag contents must
            # be reviewed by the user rather than cleared or incremented silently.
            menu = self._locator("bag_menu")
            if await menu.get_attribute("aria-expanded") != "true":
                await menu.press("Enter")
            empty = self._page().get_by_role("heading", name="你的购物袋是空的。", exact=True)
            try:
                await expect(empty).to_be_visible(timeout=3000)
            except AssertionError:
                raise HumanRequired("购物袋已有内容或无法确认，请人工检查后继续") from None
            await menu.press("Enter")
            actual = parse_skus(
                await self._snapshot(), product_id=self.product_id, model=self.selected.model
            )[0]
            if (
                actual.id != self.selected.id
                or actual.price != self.selected.price
                or not actual.available
            ):
                raise HumanRequired("加购前商品或库存已变化")
            add = self._locator("add_to_bag")
            if not await add.is_visible():
                # The pre-order Continue branch has not yet become accessible.
                raise SelectorNotFound("UNKNOWN: this Apple Continue step needs live verification")
            self._cart_attempted = True
            await add.click()
            try:
                await self._locator("view_bag").wait_for(state="visible")
            except PlaywrightTimeout:
                raise HumanRequired("加购后未出现购物袋入口，请人工核对；不会重复加购") from None
            await self._guard_human()
            await self._locator("view_bag").click()
            await self._locator("bag").wait_for(state="visible")
            # Verify the single unit added before changing quantity through its
            # real dropdown. Never repeat the add action to reach quantity 2.
            await self._bag_check(1)
            if quantity != 1:
                await self._locator("bag_quantity").select_option(str(quantity))
            await self._bag_check(quantity)
            await self._capture("bag_verified", "ADDING_CART")

    async def goto_checkout(self) -> None:
        async with self._lock:
            await self._guard_human()
            if "/shop/checkout" in urlsplit(self._page().url).path:
                return  # Resume must inspect the current checkout, not replay bag.
            await self._bag_check(self._quantity)
            await self._locator("checkout").click()
            await self._page().wait_for_url(re.compile(r"/shop/(?:signIn|checkout)(?:[/?]|$)"))
            await self._guard_human()
            await self._capture("checkout", "CHECKOUT")

    async def login_status(self) -> LoginStatus:
        async with self._lock:
            verification = await self._detect_verification()
            if verification.reason == "login":
                return LoginStatus.REQUIRED
            page = self.manager.current_page(self.platform)
            if page is None or verification.required:
                return LoginStatus.UNKNOWN
            if await page.locator(SELECTORS["authenticated"]).count():
                return LoginStatus.AUTHENTICATED
            menu = page.locator(SELECTORS["bag_menu"])
            if not await menu.is_visible():
                return LoginStatus.UNKNOWN
            opened = await menu.get_attribute("aria-expanded") != "true"
            if opened:
                await menu.press("Enter")
            try:
                for _ in range(12):
                    if await page.locator(SELECTORS["authenticated"]).count():
                        return LoginStatus.AUTHENTICATED
                    if await page.locator(SELECTORS["login_link"]).count():
                        return LoginStatus.REQUIRED
                    await asyncio.sleep(0.25)
            finally:
                if opened and await menu.get_attribute("aria-expanded") == "true":
                    await menu.press("Enter")
            return LoginStatus.UNKNOWN

    async def verify_order(self) -> OrderReview:
        async with self._lock:
            await self._guard_human()
            page = self._page()
            if "/shop/checkout" != urlsplit(page.url).path:
                raise SelectorNotFound("UNKNOWN: not on Apple checkout review")
            if (
                self.payment_method == "installments"
                and await self._locator("change_payment").is_visible()
            ):
                await self._locator("change_payment").click()
                await self._locator("payment_wechat").wait_for(state="attached")
            # Only ordinary delivery is automated. Missing or masked contact
            # inputs are left to the site's own validation, not read or stored.
            fulfillment = self._locator("fulfillment_continue")
            if await fulfillment.is_visible():
                await fulfillment.click()
                await self._locator("shipping_continue").wait_for(state="visible")
            shipping = self._locator("shipping_continue")
            if await shipping.is_visible():
                saved = self._locator("saved_address")
                if not await saved.is_visible() or not await saved.is_checked():
                    raise HumanRequired("请在官网确认收货地址和联系方式后继续")
                await shipping.click()
                try:
                    await self._locator("payment_wechat").wait_for(state="attached", timeout=5000)
                except PlaywrightTimeout:
                    raise HumanRequired("请补齐官网要求的地址、联系或发票信息后继续") from None
            payment = self._locator("payment_wechat")
            if await payment.is_visible():
                await self._guard_human()
                if self.payment_method == "installments":
                    await self._select_installments()
                else:
                    await expect(payment).to_be_enabled()
                    if not await payment.is_checked():
                        await payment.press("Space")
                        await expect(payment).to_be_checked()
                await self._locator("review_continue").click()
            await self._locator("submit_order").wait_for(state="visible")
            await self._guard_human()
            return await self._read_review()

    async def _select_installments(self):
        self._installment_verified_at = None
        if self.selected is None:
            raise HumanRequired("No selected product for installment verification")
        matches = []
        for option in await self._locator("installment_options").all():
            labels = await option.evaluate("""e=>Array.from(e.labels||[])
                .flatMap(l=>Array.from(l.querySelectorAll('img')).map(i=>i.alt))""")
            if labels == [self.installment_bank]:
                matches.append(option)
        if len(matches) != 1:
            raise HumanRequired("所选银行没有可验证的分期入口，请人工选择银行")
        bank = matches[0]
        await expect(bank).to_be_enabled()
        if not await bank.is_checked():
            await bank.press("Space")
            await expect(bank).to_be_checked()
        key = (await bank.get_attribute("data-autom")).removeprefix("checkout-billingOptions-")
        if not re.fullmatch(r"installments\d+", key):
            raise SelectorNotFound("UNKNOWN: installment control identifier changed")
        term = self._page().locator('[data-autom="' + key + '-24"]')
        await expect(term).to_be_enabled()
        label = await term.evaluate("e=>Array.from(e.labels||[]).map(l=>l.innerText).join(' ')")
        verify_installment_offer(label, self.selected.price * self._quantity)
        if not await term.is_checked():
            await term.press("Space")
            await expect(term).to_be_checked()
        self._installment_verified_at = monotonic()

    async def _read_review(self) -> OrderReview:
        sku = self.selected
        if sku is None:
            raise HumanRequired("No selected SKU to compare with order review")
        items = self._locator("bag_items")
        if await items.count() != 1:
            raise HumanRequired("订单必须仅有目标商品一行")
        item = items.first
        name = await item.locator(SELECTORS["review_name"]).inner_text()
        quantity_text = await item.locator(SELECTORS["review_quantity"]).inner_text()
        match = re.fullmatch(r"数量\s*(\d+)", quantity_text.strip())
        if normalized(name) != normalized(f"{sku.model} {sku.capacity} {sku.color}") or not match:
            raise HumanRequired("订单型号、颜色、容量或数量无法确认")
        quantity = int(match[1])
        if quantity < 1:
            raise HumanRequired("订单数量无效")
        line_total = money(await item.locator(SELECTORS["bag_price"]).inner_text())
        total = money(await self._locator("bag_total").inner_text())
        address_present = await self._page().evaluate(
            """keys => keys.every(key =>
            Array.from(document.querySelectorAll('[data-autom="form-field-'+key+'"]'))
              .some(e=>e.getClientRects().length && e.innerText.trim()))""",
            ADDRESS_FIELDS,
        )
        logos = self._locator("review_payment")
        payment = [await logo.get_attribute("alt") for logo in await logos.all()]
        if self.payment_method == "installments":
            details = normalized(await self._locator("review_payment_details").inner_text())
            if (
                payment != [self.installment_bank]
                or "24个月" not in details
                or self._installment_verified_at is None
                or monotonic() - self._installment_verified_at > 60
            ):
                raise HumanRequired("订单的24期免息方案需要重新核对")
        elif payment != ["微信支付"]:
            raise HumanRequired("订单付款方式不符合配置")
        return OrderReview(
            platform=self.platform,
            product_id=sku.product_id,
            sku_id=sku.id,
            model=sku.model,
            capacity=sku.capacity,
            color=sku.color,
            unit_price=line_total / quantity,
            total_price=total,
            quantity=quantity,
            address_present=address_present,
            verification_present=False,
            checkout_valid=await self._locator("submit_order").is_enabled(),
            line_items=1,
        )

    async def _detect_verification(self) -> Verification:
        page = self.manager.current_page(self.platform)
        if page is not None:
            if re.search(r"/shop/signin(?:/|$)", urlsplit(page.url).path, re.I):
                return Verification(required=True, reason="login")
        return await super()._detect_verification()

    async def submit_order(self) -> OrderResult:
        async with self._lock:
            self._page()
            if not self._allow_submit:
                raise HumanRequired("最终提交关闭；需要本地配置同时关闭 dry_run 并开启 auto_submit")
            if self._submit_attempted:
                return OrderResult(
                    status="UNKNOWN", message="Submission already attempted; never replay"
                )
            await self._guard_human()
            review = await self._read_review()
            prefs = self.preferences.get(self.product_id)
            if prefs is None or self.selected is None:
                raise HumanRequired("Missing configured product before submission")
            verify_checkout(review, self.selected, prefs)
            self._submit_attempted = True
            try:
                await self._locator("submit_order").click()
                await self._locator("order_confirmation").wait_for(state="visible", timeout=30_000)
                return await self._read_unpaid_receipt(after_submit=True)
            except Exception:
                # A click may already have created an order. The Engine keeps its
                # durable guard and the live browser for human reconciliation.
                return OrderResult(status="UNKNOWN", message="No conclusive unpaid order receipt")

    async def _read_unpaid_receipt(
        self, order_id: str | None = None, *, after_submit: bool = False
    ) -> OrderResult:
        unknown = OrderResult(status="UNKNOWN", message="No matching verified unpaid order receipt")
        expected = order_id if order_id is not None else self._last_order_id
        if not after_submit and (expected is None or re.fullmatch(r"W\d+", expected) is None):
            return unknown
        page = self._page()
        if urlsplit(page.url).path != "/shop/checkout/thankyou":
            return unknown
        receipt = self._locator("order_confirmation")
        heading = self._locator("payment_heading")
        if await receipt.count() != 1 or await heading.count() != 1:
            return unknown
        if not await receipt.is_visible() or not await heading.is_visible():
            return unknown
        match = re.fullmatch(r"订单\s*#(W\d+)", (await receipt.inner_text()).strip())
        if (
            match is None
            or (expected is not None and match[1] != expected)
            or "请使用你的微信扫描此二维码进行付款" not in await heading.inner_text()
        ):
            return unknown
        self._last_order_id = match[1]
        return OrderResult(
            status="SUCCESS",
            order_id=match[1],
            message="Matching unpaid order confirmed",
            payment_state="UNPAID",
        )

    async def read_order_status(self, order_id: str | None = None) -> OrderResult:
        """Read the current receipt; never navigate, resubmit, or infer a missing order."""
        async with self._lock:
            try:
                self._page()
                if (await self._detect_verification()).required:
                    return OrderResult(status="UNKNOWN", message="Order receipt needs human review")
                return await self._read_unpaid_receipt(order_id)
            except Exception:
                return OrderResult(
                    status="UNKNOWN", message="No matching verified unpaid order receipt"
                )
