from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.config import AppConfig, load_config
from src.web.api import router


def create_app(config: AppConfig | None = None, runtime=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from src.runtime import Runtime

        app.state.runtime = runtime if runtime is not None else Runtime(config or load_config())
        yield
        await app.state.runtime.close()

    app = FastAPI(title="apple-buy-bot", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def local_control(request: Request, call_next):
        host = request.headers.get("host", "")
        if urlsplit("http://" + host).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "Local host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{host}":
            return JSONResponse({"detail": "Same origin required"}, status_code=403)
        if request.method == "POST" and request.headers.get("x-apple-bot-control") != "local":
            return JSONResponse({"detail": "X-Apple-Bot-Control: local required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8")

    app.include_router(router)
    return app
