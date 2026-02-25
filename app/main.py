from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router as api_router


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "ui" / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(title="NoTasteRequired", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "ui" / "static")), name="static")
    app.include_router(api_router)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={},
        )

    return app


app = create_app()

