from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from pathlib import Path

from app.config import load_config, save_config
from app.state import get_runtime
from app.engine import NoiseWardenEngine
from app.schemas import ControlRequest

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))

cfg = load_config()
runtime = get_runtime()
engine = NoiseWardenEngine(cfg, runtime)

app = FastAPI(title="Noise Warden v2")
app.mount("/static", StaticFiles(directory=str(BASE / "web" / "static")), name="static")

@app.on_event("startup")
async def startup():
    engine.start()

@app.on_event("shutdown")
async def shutdown():
    engine.stop()

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/timeline", response_class=HTMLResponse)
async def timeline_page(request: Request):
    return templates.TemplateResponse("timeline.html", {"request": request})

@app.get("/thresholds", response_class=HTMLResponse)
async def thresholds_page(request: Request):
    return templates.TemplateResponse("thresholds.html", {"request": request})

@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request})

@app.get("/calibration", response_class=HTMLResponse)
async def calibration_page(request: Request):
    return templates.TemplateResponse("calibration.html", {"request": request})

@app.get("/api/status")
async def api_status():
    return runtime.status_payload()

@app.get("/api/incidents")
async def api_incidents(limit: int = 200, offset: int = 0):
    return {"items": runtime.db.list_incidents(limit=limit, offset=offset)}

@app.get("/api/incidents/{incident_id}")
async def api_incident(incident_id: int):
    item = runtime.db.get_incident(incident_id)
    if not item: return JSONResponse({"error": "not found"}, status_code=404)
    return item

@app.get("/api/incidents/{incident_id}/audio")
async def api_incident_audio(incident_id: int):
    item = runtime.db.get_incident(incident_id)
    if not item or not item.get("snippet_path"):
        return JSONResponse({"error": "audio not found"}, status_code=404)
    return FileResponse(item["snippet_path"], media_type="audio/wav")

@app.post("/api/incidents/{incident_id}/delete")
async def api_incident_delete(incident_id: int):
    runtime.db.delete_incident(incident_id)
    return {"ok": True}

@app.post("/api/incidents/clear")
async def api_incidents_clear():
    runtime.db.clear_incidents()
    return {"ok": True}

@app.get("/api/incidents/export.csv")
async def api_export_csv():
    path = runtime.db.export_csv()
    return FileResponse(path, media_type="text/csv", filename=Path(path).name)

@app.get("/api/timeline")
async def api_timeline(span: str = "day"):
    return {"items": runtime.db.timeline(span=span)}

@app.get("/api/thresholds")
async def api_thresholds():
    return engine.thresholds_payload()

@app.get("/api/config")
async def api_config():
    return cfg

@app.post("/api/config/save")
async def api_config_save(payload: dict):
    save_config(payload)
    return {"ok": True, "message": "Restart service to apply changes."}

@app.post("/api/control/arm")
async def api_arm(req: ControlRequest | None = None):
    runtime.armed = True
    return {"ok": True}

@app.post("/api/control/disarm")
async def api_disarm(req: ControlRequest | None = None):
    runtime.armed = False
    engine.force_stop_playback()
    return {"ok": True}

@app.post("/api/control/kill")
async def api_kill(req: ControlRequest | None = None):
    runtime.emergency_kill = True
    engine.force_stop_playback()
    return {"ok": True}

@app.post("/api/control/kill/clear")
async def api_kill_clear():
    runtime.emergency_kill = False
    return {"ok": True}

@app.post("/api/control/test_playback")
async def api_test_playback():
    engine.test_playback()
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=cfg["app"]["host"], port=cfg["app"]["port"], reload=False)
