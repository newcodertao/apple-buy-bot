import hashlib
import re
from urllib.parse import urlsplit

from src.core.exceptions import SelectorNotFound
from src.core.models import Platform, StockState
from src.platforms.marketplace import (
    MarketplaceAdapter,
    model_in_title,
    normalize,
    platform_item_id,
    visible_stock_state,
)
from src.platforms.tmall.selectors import SELECTORS


class TmallAdapter(MarketplaceAdapter):
    """Tmall channel, sharing the Alibaba browser session with Taobao."""

    platform = Platform.TMALL
    allowed_hosts = ("tmall.com", "taobao.com")
    login_hosts = ("login.taobao.com", "login.tmall.com")
    selectors = SELECTORS
    live_validation = {
        "product": "LIVE_OBSERVED",
        "cart": "BLOCKED",
        "checkout": "NOT_RUN",
        "receipt": "NOT_RUN",
    }

    async def inspect_product_details(self) -> dict:
        await self._guard_human()
        shop = self._field("shop_link").filter(visible=True)
        if await shop.count() != 1:
            raise SelectorNotFound("尚未核对当前天猫店铺的唯一公开入口")
        shop_id = urlsplit("https:" + await shop.get_attribute("href")).hostname
        region = await self._maybe_text("region")
        conditions = await self._field("price_condition").filter(visible=True).all_inner_texts()
        conditional = any(re.search(r"补|领|券|起|会员|PLUS", value, re.I) for value in conditions)
        return {
            "platform": self.platform.value,
            "platform_item_id": platform_item_id(self._page().url, self.platform),
            "title": await self._text("product_name"),
            "seller_id": shop_id or "",
            "shop_name": "Apple Store 官方旗舰店" if shop_id == "apple.tmall.com" else "",
            "region": "sha256:" + hashlib.sha256(normalize(region).encode()).hexdigest()
            if region
            else "",
            "selected_capacity": await self._maybe_text("selected_capacity"),
            "selected_color": await self._maybe_text("selected_color"),
            "displayed_price": await self._text("product_price"),
            "price_verified": not conditional,
            "stock_text": await self._maybe_text("stock"),
            "blockers": ["商品价为条件价或起步价，实际结算金额仍需确认"] if conditional else [],
        }

    async def product_snapshot(self) -> dict:
        details = await self.inspect_product_details()
        models = [
            model
            for model in self.preferences[self.product_id].model_priority
            if model_in_title(model, details["title"])
        ]
        if len(models) != 1 or not details["price_verified"]:
            raise SelectorNotFound("天猫型号或实际全价尚未确认，请核对商品与优惠条件")
        stock = details["stock_text"]
        stock_state = visible_stock_state(stock)
        available = stock_state == StockState.AVAILABLE
        return {
            "item_id": details["platform_item_id"],
            "title": details["title"],
            "model": models[0],
            "capacity": details["selected_capacity"],
            "color": details["selected_color"],
            "price": details["displayed_price"],
            "seller_id": details["seller_id"],
            "shop_name": details["shop_name"],
            "region": details["region"],
            "stock_text": stock,
            "stock_state": stock_state.value,
            "sale_mode": "PREORDER"
            if re.search(r"预售|定金|尾款", stock)
            else "NORMAL"
            if available
            else "UNKNOWN",
            "buy_enabled": await self._field("add_to_cart").is_enabled(),
        }
