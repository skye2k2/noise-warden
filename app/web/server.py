from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn, shutil, csv
from io import StringIO
from app.engine.controller import NoiseWardenController
from app.core.config import Config

Path("data/uploads").mkdir(parents=True, exist_ok=True)
Path("data/snippets").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Noise Warden v2.1")
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.mount("/uploads", StaticFiles(directory="data/uploads"), name="uploads")
app.mount("/snippets", StaticFiles(directory="data/snippets"), name="snippets")
controller = NoiseWardenController()
controller.start()

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request): return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request): return templates.TemplateResponse("timeline.html", {"request": request})

@app.get("/thresholds", response_class=HTMLResponse)
def thresholds(request: Request):
    cfg = Config.load()
    return templates.TemplateResponse("thresholds.html", {"request": request, "cfg": cfg.raw})

@app.get("/build", response_class=HTMLResponse)
def build_page(request: Request):
    return templates.TemplateResponse("build.html", {"request": request, "build": controller.store.get_build_info()})

@app.post("/build")
async def save_build(photo: UploadFile | None = File(default=None), details: str = Form(default="")):
    build = controller.store.get_build_info(); photo_path = build.get("photo_path")
    if photo and photo.filename:
        ext = Path(photo.filename).suffix or ".jpg"
        target = Path("data/uploads") / f"build_photo{ext}"
        with target.open("wb") as f: shutil.copyfileobj(photo.file, f)
        photo_path = str(target)
    controller.store.set_build_info(photo_path, details)
    return RedirectResponse("/build", status_code=303)

@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request, "cfg_text": Path("config/noise_warden.yaml").read_text(encoding="utf-8")})

@app.post("/config")
async def save_config(cfg_text: str = Form(...)):
    Path("config/noise_warden.yaml").write_text(cfg_text, encoding="utf-8")
    return RedirectResponse("/config", status_code=303)

@app.get("/api/status")
def api_status(): return JSONResponse(controller.get_status())

@app.get("/api/incidents")
def api_incidents(): return JSONResponse(controller.store.list_incidents())

@app.post("/api/arm")
def api_arm(): controller.manual_arm(True); return {"ok": True}

@app.post("/api/disarm")
def api_disarm(): controller.manual_arm(False); return {"ok": True}

@app.post("/api/incidents/{incident_id}/delete")
def delete_incident(incident_id: int): controller.store.delete_incident(incident_id); return {"ok": True}

@app.post("/api/incidents/clear")
def clear_incidents(): controller.store.clear_incidents(); return {"ok": True}

@app.get("/api/incidents/export.csv")
def export_csv():
    rows = controller.store.list_incidents(); sio = StringIO()
    if rows:
        writer = csv.DictWriter(sio, fieldnames=list(rows[0].keys())); writer.writeheader(); writer.writerows(rows)
    else:
        sio.write("id,start_ts,end_ts,initial_db,peak_db,avg_db,mode,classification,snippet_path,notes\n")
    out = Path("data/incidents_export.csv"); out.write_text(sio.getvalue(), encoding="utf-8")
    return FileResponse(out, filename="incidents_export.csv")

def run():
    cfg = Config.load()
    uvicorn.run(app, host=cfg.get("web","host", default="0.0.0.0"), port=int(cfg.get("web","port", default=8787)))
