from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.api import build_api
from app.engine import NoiseWardenEngine


def build_app(engine: NoiseWardenEngine) -> FastAPI:
    app = FastAPI(title="Noise Warden")

    static_dir = Path(engine.settings.web.static_dir)
    templates_dir = Path(engine.settings.web.templates_dir)

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(templates_dir))

    @app.get("/")
    async def home(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    app.include_router(build_api(engine))
    return app
