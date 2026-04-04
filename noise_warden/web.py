from __future__ import annotations
import os, io
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_yaml, save_yaml_text_validated, ConfigError
from .storage import Storage
from .state import StateStore
from .engine import Engine
from .ordinance import ORDINANCE

cfg = load_yaml()
storage = Storage(os.path.join(cfg["app"]["shared_dir"], "noise_warden.db"))
state = StateStore()
engine = Engine(cfg, storage, state)

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.start()
    yield
    engine.stop()

app = FastAPI(title="Noise Warden", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(static_dir, exist_ok=True)
os.makedirs(os.path.join(static_dir, "build"), exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

def auth_ok(request: Request):
    token = cfg["app"].get("auth_token") or ""
    if not token:
        return True
    return request.headers.get("Authorization") == f"Bearer {token}"

def must_auth(request: Request):
    if not auth_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

def since_for_view(view: str):
    now = datetime.now(timezone.utc)
    if view == "day":
        return (now - timedelta(days=1)).isoformat()
    if view == "week":
        return (now - timedelta(days=7)).isoformat()
    if view == "month":
        return (now - timedelta(days=30)).isoformat()
    return None

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    incidents = storage.list_incidents(limit=20)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "state": state.snapshot(),
        "incidents": incidents,
        "ordinance": ORDINANCE,
        "cfg": cfg,
        "message": request.query_params.get("msg")
    })

@app.get("/incidents", response_class=HTMLResponse)
def incidents(request: Request, page: int = 1):
    per_page = 50
    offset = (max(page,1)-1) * per_page
    total = storage.count_incidents()
    rows = storage.list_incidents(limit=per_page, offset=offset)
    return templates.TemplateResponse("incidents.html", {
        "request": request, "rows": rows, "page": page, "pages": max(1, (total + per_page - 1)//per_page)
    })

@app.post("/incidents/{incident_id}/notes")
def save_notes(request: Request, incident_id: int, notes: str = Form("")):
    must_auth(request)
    storage.update_incident_notes(incident_id, notes)
    return RedirectResponse(url="/incidents?msg=notes-saved", status_code=303)

@app.post("/incidents/{incident_id}/delete")
def delete_incident(request: Request, incident_id: int):
    must_auth(request)
    storage.soft_delete_incident(incident_id)
    return RedirectResponse(url="/incidents?msg=deleted", status_code=303)

@app.post("/incidents/clear")
def clear_all_incidents(request: Request):
    must_auth(request)
    storage.soft_delete_all_incidents()
    return RedirectResponse(url="/incidents?msg=all-cleared", status_code=303)

@app.get("/snippets/{incident_id}")
def get_snippet(request: Request, incident_id: int):
    row = storage.get_incident(incident_id)
    if not row or not row.get("snippet_path") or not os.path.exists(row["snippet_path"]):
        raise HTTPException(status_code=404)
    return StreamingResponse(open(row["snippet_path"], "rb"), media_type="audio/wav")

@app.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, view: str = "day"):
    since = since_for_view(view)
    rows = storage.list_incidents(limit=5000, since=since)
    return templates.TemplateResponse("timeline.html", {
        "request": request, "rows": rows, "view": view
    })

@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    return templates.TemplateResponse("config.html", {"request": request, "raw": raw, "message": request.query_params.get("msg")})

@app.post("/config/save")
def save_config(request: Request, raw: str = Form(...)):
    must_auth(request)
    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        save_yaml_text_validated(cfg_path, raw)
    except ConfigError as e:
        return RedirectResponse(url=f"/config?msg=error:{str(e)}", status_code=303)
    return RedirectResponse(url="/config?msg=saved-restart-required", status_code=303)

@app.get("/build", response_class=HTMLResponse)
def build_page(request: Request):
    meta = storage.get_build_meta()
    photo_path = "/static/build/build_photo.jpg" if os.path.exists(os.path.join(static_dir, "build", "build_photo.jpg")) else None
    return templates.TemplateResponse("build.html", {"request": request, "meta": meta, "photo_path": photo_path})

@app.post("/build/upload")
async def build_upload(request: Request, photo: UploadFile = File(None), notes: str = Form(""), ordinance_excerpt: str = Form("")):
    must_auth(request)
    if photo and photo.filename:
        if not photo.filename.lower().endswith((".jpg",".jpeg",".png",".webp")):
            return RedirectResponse(url="/build?msg=invalid-file", status_code=303)
        data = await photo.read()
        target = os.path.join(static_dir, "build", "build_photo.jpg")
        with open(target, "wb") as f:
            f.write(data)
    storage.save_build_meta(notes, ordinance_excerpt)
    return RedirectResponse(url="/build?msg=saved", status_code=303)

@app.get("/calibration", response_class=HTMLResponse)
def calibration(request: Request):
    return templates.TemplateResponse("calibration.html", {
        "request": request,
        "profiles": storage.list_calibration_profiles(),
        "cfg": cfg,
    })

@app.post("/calibration/compute")
async def calibration_compute(
    request: Request,
    name: str = Form(...),
    reference_spl_db: float = Form(...),
    observed_raw_dbfs: float = Form(...)
):
    must_auth(request)
    offset = reference_spl_db - observed_raw_dbfs
    storage.add_calibration_profile(name, offset, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return JSONResponse({"offset_db": offset})

@app.post("/control/pause")
def pause(request: Request):
    must_auth(request)
    engine.set_armed(False)
    return RedirectResponse(url="/?msg=paused", status_code=303)

@app.post("/control/resume")
def resume(request: Request):
    must_auth(request)
    engine.set_armed(True)
    return RedirectResponse(url="/?msg=resumed", status_code=303)

@app.get("/export.csv")
def export_csv(request: Request):
    data = storage.export_csv()
    return StreamingResponse(io.BytesIO(data.encode("utf-8")), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=incidents.csv"})

@app.get("/api/state")
def api_state():
    return JSONResponse(state.snapshot())

@app.get("/api/health")
def api_health():
    snap = state.snapshot()
    healthy = bool(snap["running"]) and bool(engine.thread and engine.thread.is_alive()) and bool(snap["mic_ok"])
    return JSONResponse({
        "ok": healthy,
        "engine_thread_alive": bool(engine.thread and engine.thread.is_alive()),
        "mic_ok": snap["mic_ok"],
        "last_error": snap["last_error"],
        "state": snap
    })
