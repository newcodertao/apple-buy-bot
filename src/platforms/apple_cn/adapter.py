import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import expect

from src.core.config import ProductPreferences, validate_platform_url
from src.core.exceptions import (
    CandidateUnavailable,
    ConfigurationError,
    HumanRequired,
    SelectorNotFound,
)
from src.core.models import (
    SKU,
    CartState,
    FinancingOffer,
    FinancingState,
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
    BAG_URL,
    LOGIN_AUTH_HOSTS,
    LOGIN_USERNAME_LABEL,
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
        self._cart_state = CartState.NOT_ATTEMPTED
        self._quantity = 1
        self._allow_submit = allow_submit
        self._submit_attempted = False
        self._last_order_id: str | None = None
        self.payment_method = payment_method
        self.installment_bank = installment_bank
        self._approved_installment = None
        self._confirmed_address = ""
        self._confirmed_market = ""
        self.purchase_plan = None
        self.bind_saved_address = None
        self._saved_address_selected = False
        self._product_target_digest = ""
        self.excluded_sku_ids: set[str] = set()

    @property
    def cart_state(self) -> CartState:
        # Preserve older callers which recorded the irreversible click intent
        # through _cart_attempted before the three-state API existed.
        if self._cart_attempted and self._cart_state == CartState.NOT_ATTEMPTED:
            return CartState.ATTEMPTED_UNKNOWN
        return self._cart_state

    def _require_plan(self, sku: SKU, quantity: int) -> None:
        if self.purchase_plan is not None:
            self.purchase_plan.require(sku, quantity, self.payment_method, self.installment_bank)

    def _address_approved(self, fingerprint: str) -> bool:
        expected = (
            self.purchase_plan.address_fingerprint
            if self.purchase_plan is not None
            else self._confirmed_address
        )
        if (
            fingerprint
            and not expected
            and self.purchase_plan is not None
            and self.purchase_plan.approved_at is not None
            and self.purchase_plan.address_basis == "selected_saved_address_first_checkout_bind"
            and self._saved_address_selected
            and self.bind_saved_address is not None
        ):
            self.bind_saved_address(fingerprint)
            expected = self.purchase_plan.address_fingerprint
        return bool(fingerprint and fingerprint == expected)

    def _market_approved(self, fingerprint: str) -> bool:
        plan = self.purchase_plan
        if (
            fingerprint
            and plan is not None
            and plan.approved_at is not None
            and plan.market_basis == "apple_cn_direct_configured_product"
            and self.selected is not None
        ):
            self._require_plan(self.selected, self._quantity)
            return any(
                p.product_id == self.product_id and p.target_digest == self._product_target_digest
                for p in plan.products
            )
        approved = (
            self.purchase_plan.market_fingerprints
            if self.purchase_plan is not None
            else [self._confirmed_market]
        )
        return bool(fingerprint and fingerprint in approved)

    @staticmethod
    def _digest(value) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _require_cn_store(self) -> None:
        """Login hosts may be allowed for auth; only the CN store can transact."""
        parts = urlsplit(self._page().url)
        host = parts.hostname or ""
        if host in {"apple.com", "www.apple.com"} and parts.path.startswith("/cn/shop/"):
            return
        cn_host = host == "www.apple.com.cn" or re.fullmatch(
            r"secure\d*\.www\.apple\.com\.cn", host
        )
        if not cn_host or not parts.path.startswith("/shop/"):
            raise HumanRequired("当前页面不是 Apple 中国大陆商店，请返回国行商品页面")

    def _validate_platform_url(self, url: str) -> None:
        super()._validate_platform_url(url)
        host = urlsplit(url).hostname or ""
        known = {
            "apple.com.cn",
            "www.apple.com.cn",
            "apple.com",
            "www.apple.com",
            "account.apple.com",
            "appleid.apple.com",
            "idmsa.apple.com",
        }
        if host not in known and not re.fullmatch(r"secure\d*\.www\.apple\.com\.cn", host):
            raise ConfigurationError("Apple 导航主机尚未验证，请人工核对")

    async def open_product(self, product_id: str, url: str) -> None:
        try:
            validate_platform_url(self.platform, url)
        except ValueError:
            raise ConfigurationError("Apple 商品入口必须属于中国大陆商店") from None
        await super().open_product(product_id, url)
        self._product_target_digest = hashlib.sha256(url.encode()).hexdigest()

    async def _address_fingerprint(self) -> str:
        self._require_cn_store()
        # The values remain process-local only long enough to hash. They never
        # enter a model, log, screenshot, database or result payload.
        values = await self._page().evaluate(
            """keys => {
                const visible=e=>e.getClientRects().length
                    && getComputedStyle(e).visibility!=='hidden';
                const fields=[...document.querySelectorAll('[data-autom^="form-field-"]')]
                    .filter(visible).map(e=>[e.getAttribute('data-autom'),e.innerText.trim()])
                    .filter(([,text])=>text);
                if(!keys.every(key=>fields.filter(([name])=>name==='form-field-'+key).length===1))
                    return null;
                const maskedContact=new Set(['form-field-emailAddress',
                    'form-field-fullDaytimePhone']);
                if(fields.some(([name,text])=>!maskedContact.has(name)
                    && /[*•●]/.test(text))) return null;
                return fields.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
            }""",
            ADDRESS_FIELDS,
        )
        if not values:
            return ""
        return self._digest(values)

    async def confirm_address(self) -> dict:
        async with self._lock:
            await self._guard_human()
            if urlsplit(self._page().url).path != "/shop/checkout":
                raise HumanRequired("请先在结算回顾页核对收货地址")
            fingerprint = await self._address_fingerprint()
            if not fingerprint:
                raise HumanRequired("当前收货地址摘要不完整，请在官网补齐并核对")
            self._confirmed_address = fingerprint
            return {"address_fingerprint": fingerprint, "address_confirmed": True}

    async def _market_fingerprint(self) -> str:
        self._require_cn_store()
        sku = self.selected
        if sku is None:
            return ""
        page = self._page()
        product = self._locator("summary").filter(visible=True)
        review = self._locator("bag_items").locator(SELECTORS["review_name"]).filter(visible=True)
        names = await (product if await product.count() else review).all_inner_texts()
        expected = normalized(f"{sku.model} {sku.capacity} {sku.color}")
        if len(names) != 1 or normalized(names[0]) != expected:
            return ""
        # Compare the exact visible item under the approved CN direct-store basis.
        # This basis is not a claim that the page exposed a CH/A part number.
        # Explicit overseas version text still contradicts the approved purchase.
        versions = await page.evaluate(
            """s => {
              const roots=[...document.querySelectorAll(s.summary),
                ...document.querySelectorAll(s.bag_items)].filter(e=>e.getClientRects().length);
              const pattern=/\\b[A-Z0-9]{4,}\\/A\\b|国行|港版|美版|日版|海外版/gi;
              return roots.flatMap(e=>(e.innerText.match(pattern)||[]));
            }""",
            SELECTORS,
        )
        if any(
            version in {"港版", "美版", "日版", "海外版"}
            or (version.upper().endswith("/A") and not version.upper().endswith("CH/A"))
            for version in versions
        ):
            return ""
        return self._digest(
            [sku.id, sku.product_id, expected, str(sku.price), sorted(set(versions))]
        )

    async def confirm_market(self) -> dict:
        async with self._lock:
            await self._guard_human()
            fingerprint = await self._market_fingerprint()
            if not fingerprint:
                raise HumanRequired("请在当前商品或结算页面核对型号、容量、颜色及国行版本")
            self._confirmed_market = fingerprint
            return {"market_evidence": "MANUAL_CN:" + fingerprint, "market_verified": True}

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
        self._require_cn_store()
        return await self._page().evaluate(PRODUCT_SNAPSHOT, SELECTORS)

    async def _choose(self, key, wanted):
        controls = self._locator(key)
        if await controls.count() == 0:
            raise SelectorNotFound("UNKNOWN: Apple " + key + " control group is missing")
        matches = []
        complete_labels = True
        for control in await controls.all():
            info = await control.evaluate(
                "e=>({value:e.value,label:Array.from(e.labels||[]).map(x=>x.innerText).join(' ')})"
            )
            label = info["label"].strip().split("\n")[0]
            complete_labels = complete_labels and bool(label)
            if normalized(info["value"]) == normalized(wanted) or normalized(label) == normalized(
                wanted
            ):
                matches.append(control)
        if not matches and complete_labels and key in {"model", "color", "capacity"}:
            raise CandidateUnavailable("当前 Apple 商品没有目标规格")
        if len(matches) != 1:
            raise SelectorNotFound("UNKNOWN: requested Apple " + key + " is missing or ambiguous")
        await self._guard_human()
        control = matches[0]
        if await control.is_disabled() and key in {"model", "color", "capacity"}:
            raise CandidateUnavailable("当前 Apple 商品目标规格不可用")
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
            if self.cart_state != CartState.NOT_ATTEMPTED or self._submit_attempted:
                raise HumanRequired("已有加购或提交尝试，不能重新切换商品规格")
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
                for model in prefs.model_priority:
                    for capacity in prefs.capacity_priority:
                        for color in prefs.color_priority:
                            try:
                                sku = await self._configure(model, capacity, color)
                            except CandidateUnavailable:
                                continue
                            if (
                                sku.id not in self.excluded_sku_ids
                                and sku.available
                                and sku.currency == "CNY"
                                and sku.price <= prefs.max_price
                                and (
                                    prefs.max_total is None
                                    or sku.price * prefs.quantity <= prefs.max_total
                                )
                            ):
                                return [sku]
                return []
            except (SelectorNotFound, PlaywrightTimeout):
                await self._capture("sku_unknown", "WAITING_HUMAN")
                raise SelectorNotFound(
                    "UNKNOWN: Apple SKU controls changed or are incomplete"
                ) from None

    async def check_stock(self) -> list[SKU]:
        return await self.get_skus()

    async def select_sku(self, sku: SKU) -> None:
        async with self._lock:
            if self.cart_state != CartState.NOT_ATTEMPTED or self._submit_attempted:
                raise HumanRequired("已有加购或提交尝试，不能重新切换商品规格")
            if sku.platform != self.platform or sku.product_id != self.product_id:
                raise HumanRequired("Selected SKU belongs to a different product")
            self._require_plan(sku, self.preferences[sku.product_id].quantity)
            actual = await self._configure(sku.model, sku.capacity, sku.color)
            if any(
                getattr(actual, field) != getattr(sku, field)
                for field in (
                    "id",
                    "platform",
                    "product_id",
                    "model",
                    "capacity",
                    "color",
                    "currency",
                )
            ):
                raise HumanRequired("Selected SKU identity changed")
            if actual.price != sku.price or not actual.available:
                raise CandidateUnavailable("当前商品价格或库存已变化，请尝试其他候选")
            self.selected = actual

    async def _bag_check(self, quantity: int):
        self._require_cn_store()
        if self.cart_state == CartState.CART_VERIFIED:
            self._cart_state = CartState.ATTEMPTED_UNKNOWN
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
        if quantity == self._quantity:
            self._cart_state = CartState.CART_VERIFIED

    async def verify_cart(self, quantity: int) -> None:
        """Recover an uncertain add by reading the existing bag, never adding again."""
        async with self._lock:
            await self._guard_human()
            self._require_cn_store()
            if quantity not in (1, 2) or self.selected is None:
                raise HumanRequired("缺少可核验的目标商品和数量")
            self._quantity = quantity
            if self.cart_state == CartState.CART_VERIFIED:
                self._cart_state = CartState.ATTEMPTED_UNKNOWN
            if not await self._locator("bag").is_visible():
                view_bag = self._locator("view_bag")
                if await view_bag.is_visible():
                    async with self.navigation_guard(self._page()):
                        await view_bag.click()
                        await self._locator("bag").wait_for(state="visible")
                else:
                    await self._navigate(self._page(), BAG_URL)
            await self._guard_human()
            await self._bag_check(quantity)

    async def add_to_cart(self, quantity: int) -> None:
        async with self._lock:
            await self._guard_human()
            if self.selected is None:
                raise HumanRequired("Select and verify a SKU before adding to bag")
            if quantity not in (1, 2):
                raise HumanRequired("Apple 已验证流程只支持页面允许的 1 至 2 件")
            self._quantity = quantity
            self._require_plan(self.selected, quantity)
            if await self._locator("bag").is_visible():
                await self._bag_check(quantity)
                return
            if self.cart_state != CartState.NOT_ATTEMPTED or self._submit_attempted:
                raise HumanRequired("已尝试加购，请人工核对购物袋；禁止重复点击")
            current_market = await self._market_fingerprint()
            if not self._market_approved(current_market):
                raise HumanRequired("请先在本机明确确认当前商品为国行版本")
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
            if actual.id != self.selected.id:
                raise HumanRequired("加购前商品身份已变化，请核对当前页面")
            if actual.price != self.selected.price or not actual.available:
                raise CandidateUnavailable("首次加购前价格或库存已变化，请检查其他候选")
            add = self._locator("add_to_bag")
            if not await add.is_visible():
                # The pre-order Continue branch has not yet become accessible.
                raise SelectorNotFound("UNKNOWN: this Apple Continue step needs live verification")
            self._cart_attempted = True
            self._cart_state = CartState.ATTEMPTED_UNKNOWN
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
            page = self.manager.current_page(self.platform)
            if page is not None and re.search(
                r"/shop/signin(?:/|$)", urlsplit(page.url).path, re.I
            ):
                return LoginStatus((await self._login_component())["status"])
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

    async def _login_component(self) -> dict:
        page = self._page()
        # Only booleans/roles are read. No usernames, values, or private errors
        # are extracted from an authentication frame.
        ready = False
        error = False
        for frame in page.frames:
            if frame is not page.main_frame:
                if (urlsplit(frame.url).hostname or "") not in LOGIN_AUTH_HOSTS:
                    continue
                try:
                    if not await (await frame.frame_element()).is_visible():
                        continue
                except Exception:
                    continue
            try:
                username = frame.get_by_role("textbox", name=LOGIN_USERNAME_LABEL, exact=True)
                if await username.count() == 1:
                    ready = ready or (await username.is_visible() and await username.is_enabled())
                container = frame.locator(SELECTORS["signin"])
                scope = container if await container.count() == 1 else frame
                error = error or await scope.get_by_role("alert").filter(visible=True).count() > 0
            except Exception:
                error = True
        if error:
            return {
                "status": "UNKNOWN",
                "component": "ERROR",
                "reason": "Apple 登录组件提示异常，请人工检查",
            }
        if ready:
            return {
                "status": "REQUIRED",
                "component": "READY",
                "reason": "登录表单已就绪，请在官网完成登录",
            }
        return {
            "status": "UNKNOWN",
            "component": "LOADING",
            "reason": "Apple 登录组件尚未就绪，请等待或人工检查网络",
        }

    async def login_status_detail(self) -> dict:
        page = self.manager.current_page(self.platform)
        if page is not None and re.search(r"/shop/signin(?:/|$)", urlsplit(page.url).path, re.I):
            async with self._lock:
                return await self._login_component()
        status = await self.login_status()
        return {"status": status.value, "component": "NOT_ON_LOGIN", "reason": "已检查当前官网会话"}

    async def try_login_from_env(self) -> dict:
        # No trustworthy password/submit selector has been observed. Do not
        # inspect environment secrets or partially type an account into a form
        # whose complete ordinary login flow has not been validated.
        return {
            "status": "NOT_RUN",
            "reason": "环境变量自动登录尚缺已验证密码和提交控件；请手动登录",
        }

    async def verify_order(self) -> OrderReview:
        async with self._lock:
            await self._guard_human()
            page = self._page()
            self._require_cn_store()
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
                self._saved_address_selected = True
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
        self._approved_installment = None
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
        if not await term.is_checked():
            await term.press("Space")
            await expect(term).to_be_checked()
        # Only the selected radio's own visible label is a plan disclosure.
        # Surrounding advertisements or another bank's terms cannot fill gaps.
        label = await term.evaluate(
            """e=>Array.from(e.labels||[])
            .filter(l=>l.getClientRects().length && getComputedStyle(l).visibility!=='hidden')
            .map(l=>l.innerText).join(' ')"""
        )
        self._approved_installment = verify_installment_offer(
            self.installment_bank + " " + label,
            self.selected.price * self._quantity,
            bank=self.installment_bank,
        )

    async def _read_review(self) -> OrderReview:
        self._require_cn_store()
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
        self._require_plan(sku, quantity)
        line_total = money(await item.locator(SELECTORS["bag_price"]).inner_text())
        total = money(await self._locator("bag_total").inner_text())
        address_fingerprint = await self._address_fingerprint()
        address_confirmed = self._address_approved(address_fingerprint)
        market_fingerprint = await self._market_fingerprint()
        market_verified = self._market_approved(market_fingerprint)
        logos = self._locator("review_payment")
        payment = [await logo.get_attribute("alt") for logo in await logos.all()]
        financing = None
        if self.payment_method == "installments":
            if payment != [self.installment_bank] or self._approved_installment is None:
                raise HumanRequired("订单的24期免息方案需要重新核对")
            details = await (
                self._locator("review_payment_details").filter(visible=True).inner_text()
            )
            current = verify_installment_offer(
                self.installment_bank + " " + details, total, bank=self.installment_bank
            )
            if current != self._approved_installment:
                raise HumanRequired("当前分期条款与已核验方案不符，请重新核对")
            financing = FinancingOffer(
                provider=current.bank,
                terms=24,
                principal=current.principal,
                total_repayment=current.total,
                interest=current.interest,
                service_fee=current.fee,
                verified_at=datetime.now(UTC),
                state=FinancingState.ELIGIBLE,
                selected=True,
            )
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
            address_present=bool(address_fingerprint),
            address_fingerprint=address_fingerprint,
            address_confirmed=address_confirmed,
            market_evidence=(
                (
                    "APPROVED_APPLE_CN_DIRECT:"
                    if self.purchase_plan is not None
                    and self.purchase_plan.market_basis == "apple_cn_direct_configured_product"
                    else "MANUAL_CN:"
                )
                + market_fingerprint
                if market_verified
                else ""
            ),
            market_verified=market_verified,
            verification_present=False,
            checkout_valid=await self._locator("submit_order").is_enabled(),
            line_items=1,
            financing=financing,
        )

    async def _detect_verification(self) -> Verification:
        page = self.manager.current_page(self.platform)
        if page is not None:
            if re.search(r"/shop/signin(?:/|$)", urlsplit(page.url).path, re.I):
                component = (await self._login_component())["component"]
                reason = {"READY": "login", "LOADING": "login_loading", "ERROR": "login_error"}[
                    component
                ]
                return Verification(required=True, reason=reason)
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
        self._require_cn_store()
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
