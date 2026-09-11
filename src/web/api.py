from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from src.core.config import ProductTarget
from src.core.exceptions import BotError
from src.core.logging import safe_url
from src.core.models import Platform

router = APIRouter()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platforms: list[Platform] | None = None
    immediate: bool = False
    dry_run: Literal[True] | None = None
    # No remote option can disable dry_run or enable auto_submit.


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform | None = None


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform = Platform.APPLE


class ProductRequest(SessionRequest):
    product_id: str


class TargetRequest(ProductRequest):
    target: ProductTarget


@router.get("/status")
async def status(request: Request):
    return request.app.state.runtime.snapshot()


@router.get("/platforms")
async def platforms(request: Request):
    runtime = request.app.state.runtime
    return {
        "configured": runtime.config.platforms,
        "current": runtime.engine.snapshot().get("platforms", {}),
    }


@router.get("/products")
async def products(request: Request):
    config = request.app.state.runtime.config
    data = {key: value.model_dump(mode="json") for key, value in config.products.items()}
    for product in data.values():
        for target in product["platforms"].values():
            raw = target["url"]
            parts = urlsplit(raw)
            # Product ids are public and needed when editing Tmall/Taobao URLs.
            # Authentication, referral and arbitrary query values are never returned.
            query = urlencode(
                [
                    (key, value)
                    for key, value in parse_qsl(parts.query)
                    if key in {"id", "skuId"} and value.isdigit()
                ]
            )
            path = (
                parts.path
                if parts.path.rsplit("/", 1)[-1].removesuffix(".html").isdigit()
                else urlsplit(safe_url(raw)).path
            )
            target["url"] = urlunsplit((parts.scheme, parts.hostname or "", path, query, ""))
    return data


@router.get("/events")
async def events(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    return request.app.state.runtime.database.recent("events", limit)


@router.get("/orders")
async def orders(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    rows = request.app.state.runtime.database.recent("orders", limit)
    for row in rows:
        row.pop("review_json", None)
        if row.get("order_id"):
            row["order_id"] = "***" + row["order_id"][-4:]
    return rows


@router.get("/health")
async def health(request: Request):
    runtime = request.app.state.runtime
    runtime.database.recent("runs", 1)
    return {
        "status": "ok",
        "running": runtime.snapshot()["running"],
        "selector_validation": "APPLE_PRODUCT_BAG_REVIEW_RECEIPT_VERIFIED",
        "real_checkout_test": "CHROME_UNPAID_ORDER_PASS_2026_09_10",
        "program_end_to_end_test": "BLOCKED_LIVE_BAG_404_DELIVERY_541_2026_09_10",
        "marketplace_validation": {
            platform.value: {
                **getattr(runtime.adapters[platform], "live_validation", {}),
                "automatic_order": "NOT_VERIFIED",
            }
            for platform in (Platform.JD, Platform.TMALL, Platform.TAOBAO)
        },
    }


@router.post("/start")
async def start(request: Request, body: StartRequest):
    try:
        return await request.app.state.runtime.start(body.platforms, body.immediate, body.dry_run)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/stop")
async def stop(request: Request):
    return await request.app.state.runtime.stop()


@router.post("/login")
async def login(request: Request, body: SessionRequest):
    try:
        return await request.app.state.runtime.open_login(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/check-login")
async def check_login(request: Request, body: SessionRequest):
    try:
        return await request.app.state.runtime.check_login(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/resume")
async def resume(request: Request, body: ResumeRequest):
    try:
        return await request.app.state.runtime.resume(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/check-product")
async def check_product(request: Request, body: ProductRequest):
    try:
        return await request.app.state.runtime.check_product(body.platform, body.product_id)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/save-target")
async def save_target(request: Request, body: TargetRequest):
    try:
        return await request.app.state.runtime.save_target(
            body.platform, body.product_id, body.target
        )
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/read-order")
async def read_order(request: Request, body: SessionRequest):
    try:
        return await request.app.state.runtime.read_order(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/check-checkout")
async def check_checkout(request: Request, body: SessionRequest):
    try:
        return await request.app.state.runtime.inspect_checkout(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None
