from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Platform(StrEnum):
    APPLE = "apple"
    JD = "jd"
    TMALL = "tmall"
    TAOBAO = "taobao"


class StockState(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class SaleMode(StrEnum):
    UNKNOWN = "UNKNOWN"
    NORMAL = "NORMAL"
    PREORDER = "PREORDER"
    RESERVATION = "RESERVATION"


class FinancingState(StrEnum):
    UNKNOWN = "UNKNOWN"
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"


class FinancingOffer(Model):
    provider: str = ""
    terms: int | None = Field(default=None, gt=0)
    interest: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    service_fee: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    total_repayment: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    principal: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    verified_at: datetime | None = None
    stage: Literal["PRE_ORDER", "POST_ORDER"] = "PRE_ORDER"
    state: FinancingState = FinancingState.UNKNOWN
    selected: bool = False


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


class CartState(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    ATTEMPTED_UNKNOWN = "ATTEMPTED_UNKNOWN"
    CART_VERIFIED = "CART_VERIFIED"


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
    seller_id: str = ""
    shop_name: str = ""
    platform_item_id: str = ""
    region: str = ""
    observed_at: datetime | None = None
    stock_state: StockState = StockState.UNKNOWN
    sale_mode: SaleMode = SaleMode.UNKNOWN


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
    address_fingerprint: str = ""
    address_confirmed: bool = False
    market_evidence: str = ""
    market_verified: bool = False
    verification_present: bool = False
    checkout_valid: bool
    # Count ALL line items, including accessories, before allowing submit.
    line_items: int = Field(ge=0)
    seller_id: str = ""
    shop_name: str = ""
    platform_item_id: str = ""
    region: str = ""
    observed_at: datetime | None = None
    stock_state: StockState = StockState.UNKNOWN
    sale_mode: SaleMode = SaleMode.UNKNOWN
    items_subtotal: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    discount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    shipping: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    fees: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    financing: FinancingOffer | None = None


class OrderResult(Model):
    status: str  # SUCCESS / REJECTED / UNKNOWN; UNKNOWN must never be retried.
    order_id: str | None = None
    message: str = ""
    payment_state: str = "UNKNOWN"
    financing_state: str = "UNKNOWN"


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
