from abc import ABC, abstractmethod
from pathlib import Path

from src.core.exceptions import HumanRequired
from src.core.models import SKU, LoginStatus, OrderResult, OrderReview, Platform, Verification


class PlatformAdapter(ABC):
    """The engine depends only on this interface, never on a browser DOM."""

    platform: Platform

    @abstractmethod
    async def login_status(self) -> LoginStatus: ...

    @abstractmethod
    async def open_product(self, product_id: str, url: str) -> None: ...

    @abstractmethod
    async def get_skus(self) -> list[SKU]: ...

    @abstractmethod
    async def check_stock(self) -> list[SKU]: ...

    @abstractmethod
    async def select_sku(self, sku: SKU) -> None: ...

    @abstractmethod
    async def add_to_cart(self, quantity: int) -> None: ...

    @abstractmethod
    async def goto_checkout(self) -> None: ...

    @abstractmethod
    async def verify_order(self) -> OrderReview: ...

    @abstractmethod
    async def submit_order(self) -> OrderResult: ...

    async def read_order_status(self, order_id: str | None = None) -> OrderResult:
        """Read the current receipt only; absence never proves no order exists."""
        return OrderResult(status="UNKNOWN", message="当前页面尚无可核对的订单回执")

    async def confirm_address(self) -> dict:
        raise HumanRequired("当前页面没有可绑定的收货地址，请在结算页核对")

    async def confirm_market(self) -> dict:
        raise HumanRequired("当前页面没有可绑定的商品版本，请在商品或结算页核对国行版本")

    @abstractmethod
    async def detect_verification(self) -> Verification: ...

    @abstractmethod
    async def capture(self, action: str, state: str = "") -> Path | None: ...

    @abstractmethod
    async def close(self) -> None: ...
