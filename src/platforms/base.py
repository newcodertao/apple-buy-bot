from abc import ABC, abstractmethod
from pathlib import Path

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

    @abstractmethod
    async def detect_verification(self) -> Verification: ...

    @abstractmethod
    async def capture(self, action: str, state: str = "") -> Path | None: ...

    @abstractmethod
    async def close(self) -> None: ...
