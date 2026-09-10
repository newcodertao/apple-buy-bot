from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, PrivateAttr, field_validator, model_validator

from src.core.exceptions import ConfigurationError
from src.core.models import Model, Platform

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"
ALLOWED_HOSTS = {
    Platform.APPLE: ("apple.com", "apple.com.cn"),
    Platform.JD: ("jd.com",),
    Platform.TMALL: ("tmall.com",),
}


def validate_platform_url(platform: Platform, url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS[platform])
    ):
        raise ValueError(f"{platform}: expected an HTTPS URL on its official domain")
    return url


class AppSettings(Model):
    dry_run: bool = True
    headless: bool = False


class SaleSettings(Model):
    time: datetime | None = None
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    def target(self) -> datetime | None:
        if self.time is None:
            return None
        zone = ZoneInfo(self.timezone)
        if self.time.tzinfo is None:
            return self.time.replace(tzinfo=zone)
        return self.time.astimezone(zone)


class ProductPreferences(Model):
    model_priority: list[str] = Field(
        default_factory=lambda: ["iPhone 18 Pro Max", "iPhone 18 Pro"]
    )
    capacity_priority: list[str] = Field(default_factory=lambda: ["512GB", "256GB", "1TB"])
    color_priority: list[str] = Field(
        default_factory=lambda: ["黑色", "银色", "冰川蓝色", "勃艮第酒红色"]
    )
    quantity: int = Field(default=1, ge=1, le=100)
    max_price: Decimal = Field(default=Decimal("15000"), gt=0, allow_inf_nan=False)

    @field_validator("model_priority", "capacity_priority", "color_priority")
    @classmethod
    def priorities(cls, values: list[str]) -> list[str]:
        if not values or any(not x.strip() for x in values) or len(set(values)) != len(values):
            raise ValueError("Priorities must be nonempty, unique, nonblank values")
        return values


class ProductTarget(Model):
    url: str = ""


class ProductConfig(Model):
    model: str = Field(min_length=1)
    platforms: dict[Platform, ProductTarget] = Field(default_factory=dict)
    capacity_priority: list[str] | None = None
    color_priority: list[str] | None = None
    max_price: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    quantity: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_urls(self):
        for platform, target in self.platforms.items():
            if target.url:
                validate_platform_url(platform, target.url)
        return self


class PlatformSettings(Model):
    enabled: bool = True


class MonitorSettings(Model):
    normal_interval: float = Field(default=3.0, ge=2, le=60)
    warmup_interval: float = Field(default=1.0, ge=0.3, le=60)
    sale_interval: float = Field(default=0.5, ge=0.3, le=60)
    jitter: float = Field(default=0.1, ge=0, le=0.5)
    max_retries: int = Field(default=5, ge=0, le=100)
    backoff_max: float = Field(default=30.0, ge=1, le=300)
    max_checks: int = Field(default=120, ge=1, le=10000)
    sale_window_seconds: float = Field(default=60.0, ge=0, le=600)


class OrderSettings(Model):
    auto_submit: bool = False
    single_order_lock: bool = True
    mode: str = "race"
    payment_method: Literal["installments", "wechat"] = "installments"
    installment_bank: str = Field(default="中国建设银行", min_length=1, max_length=80)

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in {"race", "parallel"}:
            raise ValueError("mode must be race or parallel")
        return value

    @field_validator("single_order_lock")
    @classmethod
    def required_lock(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Phase 1 always requires the single order lock")
        return value


class Paths(Model):
    root: Path

    @property
    def profiles(self) -> Path:
        return self.root / "data" / "profiles"

    @property
    def database(self) -> Path:
        return self.root / "data" / "database.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def screenshots(self) -> Path:
        return self.root / "screenshots"


class AppConfig(Model):
    app: AppSettings = Field(default_factory=AppSettings)
    sale: SaleSettings = Field(default_factory=SaleSettings)
    product: ProductPreferences = Field(default_factory=ProductPreferences)
    products: dict[str, ProductConfig] = Field(default_factory=dict)
    platforms: dict[Platform, PlatformSettings] = Field(
        default_factory=lambda: {p: PlatformSettings() for p in Platform}
    )
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    order: OrderSettings = Field(default_factory=OrderSettings)
    _root: Path = PrivateAttr(default=PROJECT_ROOT)

    @property
    def paths(self) -> Paths:
        return Paths(root=self._root)

    def preferences_for(self, product_id: str) -> ProductPreferences:
        target = self.products[product_id]
        values = self.product.model_dump()
        values["model_priority"] = [target.model]
        for key in ("capacity_priority", "color_priority", "max_price", "quantity"):
            if getattr(target, key) is not None:
                values[key] = getattr(target, key)
        return ProductPreferences.model_validate(values)

    @model_validator(mode="after")
    def valid_products(self):
        for key, product in self.products.items():
            if not key or product.model not in self.product.model_priority:
                raise ValueError("Every product model must be in product.model_priority")
            self.preferences_for(key)
        return self

    def targets(self, platforms=None) -> list[tuple[Platform, str, str]]:
        selected = set(platforms if platforms is not None else Platform)
        ordered = sorted(
            self.products.items(), key=lambda item: self.product.model_priority.index(item[1].model)
        )
        return [
            (platform, key, target.url)
            for key, product in ordered
            for platform, target in product.platforms.items()
            if platform in selected
            and self.platforms.get(platform, PlatformSettings(enabled=False)).enabled
            and target.url
        ]


def load_config(path: Path = DEFAULT_CONFIG) -> AppConfig:
    if not path.exists():
        raise ConfigurationError("Missing config.yaml; run: python -m src.main init")
    try:
        with path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        config = AppConfig.model_validate(value)
        # All runtime paths are anchored to config's project root, never caller cwd.
        config._root = path.resolve().parent.parent
        return config
    except (ValueError, yaml.YAMLError, OSError) as exc:
        # Validation errors may echo user-supplied credentials; do not print the raw error.
        raise ConfigurationError(
            "Invalid configuration; check schema, priorities and URLs"
        ) from exc
