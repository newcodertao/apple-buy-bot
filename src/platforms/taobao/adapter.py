from urllib.parse import urlsplit

from src.core.exceptions import HumanRequired
from src.core.models import Platform
from src.platforms.marketplace import MarketplaceAdapter
from src.platforms.taobao.selectors import SELECTORS


class TaobaoAdapter(MarketplaceAdapter):
    """Keep Taobao item identity separate from Tmall despite the shared login."""

    platform = Platform.TAOBAO
    allowed_hosts = ("taobao.com", "tmall.com")
    login_hosts = ("login.taobao.com", "login.tmall.com")
    selectors = SELECTORS
    live_validation = {
        "product": "BLOCKED",
        "cart": "NOT_RUN",
        "checkout": "NOT_RUN",
        "receipt": "NOT_RUN",
    }

    async def product_snapshot(self) -> dict:
        host = urlsplit(self._page().url).hostname or ""
        if host == "tmall.com" or host.endswith(".tmall.com"):
            raise HumanRequired("这是天猫商品，请配置到天猫渠道，避免跨入口重复购买")
        return await super().product_snapshot()
