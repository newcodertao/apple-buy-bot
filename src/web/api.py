from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

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
            target["url"] = safe_url(target["url"])
    return data


@router.get("/events")
async def events(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    return request.app.state.runtime.database.recent("events", limit)


@router.get("/orders")
async def orders(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    return request.app.state.runtime.database.recent("orders", limit)


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
        "jd_tmall_validation": "UNKNOWN",
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
