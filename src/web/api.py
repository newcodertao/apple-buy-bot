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
    # No remote option can disable dry_run or enable auto_submit.


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform | None = None


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
        "selector_validation": "UNKNOWN",
        "real_checkout_test": "NOT RUN",
    }


@router.post("/start")
async def start(request: Request, body: StartRequest):
    try:
        return await request.app.state.runtime.start(body.platforms, body.immediate)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/stop")
async def stop(request: Request):
    return await request.app.state.runtime.stop()


@router.post("/resume")
async def resume(request: Request, body: ResumeRequest):
    try:
        return await request.app.state.runtime.resume(body.platform)
    except BotError as exc:
        raise HTTPException(409, str(exc)) from None
