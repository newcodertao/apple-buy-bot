import re
from decimal import Decimal
from urllib.parse import urlsplit

from src.core.exceptions import HumanRequired, SelectorNotFound
from src.core.models import Platform, StockState
from src.platforms.jd.selectors import SELECTORS
from src.platforms.marketplace import (
    MarketplaceAdapter,
    model_in_title,
    normalize,
    platform_item_id,
    visible_stock_state,
)


def has_installment_interest(text: str) -> bool:
    amounts = re.findall(r"含利息\s*[￥¥]\s*(\d+(?:,\d{3})*(?:\.\d+)?)", text)
    return any(Decimal(amount.replace(",", "")) > 0 for amount in amounts)


class JDAdapter(MarketplaceAdapter):
    """JD channel; selection and receipt checks use the shared transaction flow."""

    platform = Platform.JD
    allowed_hosts = ("jd.com",)
    login_hosts = ("passport.jd.com", "plogin.m.jd.com")
    selectors = SELECTORS
    live_validation = {
        "product": "LIVE_OBSERVED",
        "cart": "LIVE_OBSERVED",
        "checkout": "LIVE_OBSERVED_PAYMENT_COMBINED",
        "receipt": "NOT_RUN",
    }
    navigation_hosts = {
        "open_cart": ("item.jd.com", "cart.jd.com", "passport.jd.com"),
    }

    async def _checkout_details(self) -> dict:
        page = self._page()
        frame_element = page.locator('iframe[src^="https://pc-settlement-lite-pro.pf.jd.com"]')
        if await frame_element.count() != 1:
            raise SelectorNotFound("请在京东商品页打开订单确认窗口后再检查")
        frame = frame_element.content_frame

        async def text(selector):
            elements = frame.locator(selector).filter(visible=True)
            return (await elements.inner_text()).strip() if await elements.count() == 1 else ""

        action = await text('[class^="submit-"]')
        plans = frame.locator('[class^="modePayment_main-"]').filter(visible=True)
        term24 = []
        for plan in await plans.all():
            terms = plan.locator('[class^="planFeeInfo-"]')
            if await terms.count() and "24期" in await terms.inner_text():
                fee = plan.locator('[class^="planFeeDoc-"]')
                term24.append(
                    {
                        "amount": (await terms.inner_text()).strip(),
                        "fees": (await fee.inner_text()).strip() if await fee.count() else "",
                    }
                )
        blockers = []
        if "支付" in action or "付款" in action:
            blockers.append("该入口将直接付款，程序保留页面交由你处理")
        charged = any(has_installment_interest(plan["fees"]) for plan in term24)
        if charged:
            blockers.append("当前24期方案含利息，不符合免息偏好")
        return {
            "status": "结算检查完成",
            "platform": self.platform.value,
            "product": await text('[class*="text-d421ea"]'),
            "unit_price": await text('[class^="received-price-"]'),
            "items_subtotal": await text(".bill-item-total .bill-item__price"),
            "total_price": await text('[class^="submitOrder-"] [class^="price-"]'),
            "final_action": action,
            "installment_options": term24,
            "financing_state": "INELIGIBLE" if charged else "UNKNOWN",
            "blockers": blockers,
        }

    async def inspect_checkout(self) -> dict:
        async with self._lock:
            return await self._checkout_details()

    async def verify_order(self):
        page = self._page()
        if await page.locator('iframe[src^="https://pc-settlement-lite-pro.pf.jd.com"]').count():
            async with self._lock:
                details = await self._checkout_details()
                raise HumanRequired(
                    "；".join(details["blockers"]) or "当前结算窗口的订单身份与费用仍需人工核对"
                )
        return await super().verify_order()

    async def _shop_id(self, item_id: str, name: str) -> str:
        binding = getattr(self, "_seller_binding", None)
        if binding and binding[:2] == (item_id, name):
            return binding[2]
        self._seller_binding = None
        page = self._page()
        popup = None
        try:
            async with self.navigation_guard(
                page,
                allowed_hosts=(urlsplit(page.url).hostname, *self.login_hosts),
                allowed_popup_hosts=("mall.jd.com",),
            ):
                async with page.expect_popup(timeout=8000) as event:
                    await self._field("shop_name").click()
                popup = await event.value
                await popup.wait_for_load_state("domcontentloaded")
            parsed = urlsplit(popup.url)
            match = re.fullmatch(r"/index-(\d+)\.html", parsed.path)
            if (
                parsed.hostname != "mall.jd.com"
                or not match
                or normalize(await popup.title()) != normalize(name + " - 京东")
                or platform_item_id(page.url, self.platform) != item_id
                or await self._text("shop_name") != name
            ):
                raise HumanRequired("店铺公开入口与当前商品无法对应")
            self._seller_binding = (item_id, name, match[1])
            return match[1]
        finally:
            if popup is not None:
                await popup.close()

    async def inspect_product_details(self) -> dict:
        """Expose only public identity and the opaque region ID, even if price is conditional."""
        await self._guard_human()
        name = await self._text("shop_name")
        item_id = platform_item_id(self._page().url, self.platform)
        region = self._field("region").filter(visible=True)
        region_id = await region.get_attribute("data-id") if await region.count() == 1 else ""
        conditions = await self._field("price_condition").filter(visible=True).all_inner_texts()
        conditional = any(
            re.search(r"补贴|领后|券后|到手|返|PLUS|会员", s, re.I) for s in conditions
        )
        return {
            "platform": self.platform.value,
            "platform_item_id": item_id,
            "title": await self._text("product_name"),
            "seller_id": await self._shop_id(item_id, name),
            "shop_name": name,
            "region": region_id or "",
            "selected_capacity": await self._text("selected_capacity"),
            "selected_color": await self._text("selected_color"),
            "displayed_price": await self._text("product_price"),
            "price_verified": not conditional,
            "blockers": ["页面展示补贴或领券条件价，尚未确认实际应付金额"] if conditional else [],
        }

    async def product_snapshot(self) -> dict:
        details = await self.inspect_product_details()
        title = details["title"]
        # iPhone 17 Pro must not match the longer Pro Max title.
        models = self.preferences[self.product_id].model_priority
        matches = [model for model in models if model_in_title(model, title)]
        if len(matches) != 1:
            raise SelectorNotFound("UNKNOWN: 京东商品型号与配置无法唯一对应")
        price = details["displayed_price"]
        if not details["price_verified"]:
            # The observed label is conditional; neither it nor the struck price
            # proves the current account's payable amount.
            raise SelectorNotFound("UNKNOWN: 京东当前展示补贴或领券条件价，请人工确认实际报价")
        purchase = await self._maybe_text("selected_purchase")
        stock = await self._maybe_text("stock")
        stock_state = visible_stock_state(stock)
        normal = purchase == "公开版" and stock_state == StockState.AVAILABLE
        return {
            "item_id": platform_item_id(self._page().url, self.platform),
            "title": title,
            "model": matches[0],
            "price": price,
            "capacity": await self._text("selected_capacity"),
            "color": await self._text("selected_color"),
            "seller_id": details["seller_id"],
            "shop_name": details["shop_name"],
            "region": details["region"],
            "stock_text": stock,
            "stock_state": stock_state.value
            if stock_state == StockState.UNAVAILABLE or normal
            else "UNKNOWN",
            "sale_mode": "PREORDER"
            if re.search(r"预售|定金|尾款", purchase + stock)
            else "NORMAL"
            if normal
            else "UNKNOWN",
            "buy_enabled": await self._field("add_to_cart").is_enabled(),
        }
