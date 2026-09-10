from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Platform(StrEnum):
    APPLE = "apple"
    JD = "jd"
    TMALL = "tmall"


class State(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    WAITING = "WAITING"
    MONITORING = "MONITORING"
    STOCK_FOUND = "STOCK_FOUND"
    SELECTING_SKU = "SELECTING_SKU"
    ADDING_CART = "ADDING_CART"
    CHECKOUT = "CHECKOUT"
    VERIFYING = "VERIFYING"
    WAITING_HUMAN = "WAITING_HUMAN"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTING = "SUBMITTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    STOPPED = "STOPPED"


class LoginStatus(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    REQUIRED = "REQUIRED"
    UNKNOWN = "UNKNOWN"


class SKU(Model):
    id: str = Field(min_length=1)
    platform: Platform
    product_id: str
    model: str
    capacity: str
    color: str
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    available: bool
    currency: str = "CNY"
    delivery: str | None = None
    pickup: str | None = None


class OrderReview(Model):
    platform: Platform
    product_id: str
    sku_id: str
    model: str
    capacity: str
    color: str
    unit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    total_price: Decimal = Field(gt=0, allow_inf_nan=False)
    quantity: int = Field(gt=0)
    currency: str = "CNY"
    address_present: bool
    verification_present: bool = False
    checkout_valid: bool
    # Count ALL line items, including accessories, before allowing submit.
    line_items: int = Field(ge=0)


class OrderResult(Model):
    status: str  # SUCCESS / REJECTED / UNKNOWN; UNKNOWN must never be retried.
    order_id: str | None = None
    message: str = ""


class Verification(Model):
    required: bool = False
    reason: str = ""


class PlatformStatus(BaseModel):
    platform: Platform
    product_id: str = ""
    state: State = State.IDLE
    login: LoginStatus = LoginStatus.UNKNOWN
    sku: SKU | None = None
    stock: bool | None = None
    last_check: datetime | None = None
    retries: int = 0
    result: str = ""
    timings: dict[str, float] = Field(default_factory=dict)
