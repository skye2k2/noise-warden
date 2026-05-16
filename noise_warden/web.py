from __future__ import annotations
import json, os, io, signal, threading
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_yaml, save_yaml_text_validated, ConfigError
from .storage import Storage
from .state import StateStore
from .engine import Engine
from .ordinance import ORDINANCE, applicable_threshold, is_night
from .reclassify import analyze_clip, reclassify_all
from .seed import seed_all, DEFAULT_CLASSIFICATION_DIR

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

def _running_under_systemd():
    """Detect whether we're running as a systemd service. systemd sets
    INVOCATION_ID for every unit it manages — its absence means we're
    running standalone (local dev, manual uvicorn, etc.)."""
    return bool(os.environ.get("INVOCATION_ID"))

def auth_ok(request: Request):
    token = cfg["app"].get("auth_token") or ""
    if not token:
        return True
    return request.headers.get("Authorization") == f"Bearer {token}"

def must_auth(request: Request):
    if not auth_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

def since_for_view(view: str):
    now = datetime.now().astimezone()
    if view == "day":
        return (now - timedelta(days=1)).isoformat()
    if view == "week":
        return (now - timedelta(days=7)).isoformat()
    if view == "month":
        return (now - timedelta(days=30)).isoformat()
    return None

def _persist_armed(armed: bool):
    """Write the armed state to the YAML config so it survives watch-mode reloads."""
    import re
    cfg["detection"]["armed"] = armed
    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        val = "true" if armed else "false"
        if "armed:" in raw:
            updated = re.sub(r"(armed:\s*)\S+", rf"\g<1>{val}", raw, count=1)
        else:
            # Insert after the mode: line in the detection block
            updated = re.sub(
                r"(mode:\s*\S+[^\n]*\n)",
                rf"\g<1>  armed: {val}\n",
                raw,
                count=1
            )
        save_yaml_text_validated(cfg_path, updated)
    except Exception as e:
        print(f"[web] Failed to persist armed state: {e}")

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    incidents = storage.list_incidents(limit=20)
    return templates.TemplateResponse(request, "dashboard.html", {
        "state": state.snapshot(),
        "incidents": incidents,
        "ordinance": ORDINANCE,
        "ordinance_json": json.dumps(ORDINANCE),
        "borderline_margin_db": float(cfg["detection"].get("borderline_margin_db", 5.0)),
        "cfg": cfg,
        "has_systemd": _running_under_systemd(),
    })

@app.get("/incidents", response_class=HTMLResponse)
def incidents(request: Request, page: int = 1):
    per_page = 50
    offset = (max(page,1)-1) * per_page
    total = storage.count_incidents()
    rows = storage.list_incidents(limit=per_page, offset=offset)
    return templates.TemplateResponse(request, "incidents.html", {
        "rows": rows, "page": page, "pages": max(1, (total + per_page - 1)//per_page),
        "ordinance_json": json.dumps(ORDINANCE),
        "borderline_margin_db": float(cfg["detection"].get("borderline_margin_db", 5.0)),
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
    snippets_dir = os.path.join(cfg["app"]["shared_dir"], "snippets")
    removed = storage.hard_clear_all_incidents(snippets_dir)
    print(f"[web] Hard-cleared all incidents, removed {removed} snippet file(s)")
    return RedirectResponse(url="/incidents?msg=all-cleared", status_code=303)

@app.post("/incidents/seed")
def seed_from_classification_data(request: Request):
    """Re-seed the database from classification_data WAV files.

    Only allowed when the incidents table is empty — prevents accidental
    duplication of seeded rows alongside real incident data.
    """
    must_auth(request)
    # Guard against seeding when ANY incidents exist (including excluded ones)
    if storage.count_incidents(include_excluded=True) > 0:
        return RedirectResponse(url="/incidents?msg=seed-blocked", status_code=303)

    snippets_dir = os.path.join(cfg["app"]["shared_dir"], "snippets")
    results = seed_all(
        storage, DEFAULT_CLASSIFICATION_DIR,
        cfg["detection"], cfg["audio"], cfg, snippets_dir,
    )
    count = len(results)
    print(f"[web] Seeded {count} incident(s) from classification data")
    return RedirectResponse(url=f"/incidents?msg=seeded-{count}", status_code=303)


@app.post("/incidents/{incident_id}/reclassify")
def reclassify_incident_api(request: Request, incident_id: int, apply: bool = False):
    """Re-run the DSP pipeline on an incident's snippet with current config thresholds.

    Returns a JSON comparison of old vs new classification. With ?apply=true,
    writes the new classification and journal back to the database.
    """
    must_auth(request)

    inc = storage.get_incident(incident_id)
    if not inc:
        raise HTTPException(404, "Incident not found")

    wav_path = inc.get("snippet_path")
    if not wav_path or not os.path.exists(wav_path):
        raise HTTPException(404, "No audio snippet available for this incident")

    result = analyze_clip(wav_path, cfg["detection"], cfg["audio"], engine_captured=True)

    old_class = inc.get("classification") or "unknown"
    new_class = result["dominant"]
    class_changed = old_class != new_class

    # Compare journals to detect timeline-only changes (e.g. backdate shifts).
    # analyze_clip() returns journal entries as tuples, but JSON round-trips
    # them as lists. Normalize new_journal to lists so the comparison works.
    import json as _json
    old_journal_raw = inc.get("class_journal")
    old_journal = _json.loads(old_journal_raw) if old_journal_raw else []
    new_journal = [list(entry) for entry in result["journal"]]
    journal_changed = old_journal != new_journal

    changed = class_changed or journal_changed

    if apply and changed:
        journal_json = _json.dumps(new_journal)
        new_class_to_store = new_class if class_changed else old_class
        with storage.conn() as c:
            c.execute(
                "UPDATE incidents SET classification=?, class_journal=? WHERE id=?",
                (new_class_to_store, journal_json, incident_id)
            )

    return JSONResponse({
        "incident_id": incident_id,
        "old_classification": old_class,
        "new_classification": new_class,
        "changed": changed,
        "class_changed": class_changed,
        "journal_changed": journal_changed,
        "applied": apply and changed,
        "journal": result["journal"],
        "filter_counts": result["filter_counts"],
        "n_blocks": result["n_blocks"],
        "peak_db": result["peak_db"],
        "avg_db": result["avg_db"],
    })

@app.post("/incidents/reclassify-all")
def reclassify_all_api(request: Request, apply: bool = False):
    """Re-run the DSP pipeline on every incident with a snippet WAV.

    Returns a JSON summary of what changed. With ?apply=true, writes
    updated classifications, journals, beat_confidence, and music_like_score
    back to the database. Can take several seconds for large databases —
    the client should indicate progress.
    """
    must_auth(request)
    result = reclassify_all(storage, cfg["detection"], cfg["audio"], update=apply)
    print(f"[web] Reclassify-all: {result['processed']} processed, "
          f"{len(result['changed'])} changed, applied={apply}")
    return JSONResponse(result)

@app.get("/snippets/{incident_id}")
def get_snippet(request: Request, incident_id: int):
    """Serve an incident's WAV audio snippet with HTTP Range request support.

    WHY THIS IS NOT A SIMPLE FileResponse:
    ───────────────────────────────────────
    Browser <audio> elements require Range request support for scrubbing/seeking.
    When a user clicks the seek bar, the browser sends "Range: bytes=X-Y" and
    expects a 206 Partial Content response with Content-Range headers. Starlette's
    FileResponse (as of 0.38.x) returns 200 with the full file regardless,
    which causes the browser to:
      - Treat the audio as a non-seekable stream
      - Reset the playhead to the start on any seek attempt
      - Fail to display the correct duration until the entire file downloads

    The Service Worker's maybeSliceForRange() handles this for CACHED snippets,
    but on the first load (before the SW caches the response), the browser talks
    directly to this route. Both layers are needed:
      - This route:  handles Range for first-load / uncached requests
      - The SW:      handles Range for cached / offline requests

    DO NOT replace this with FileResponse without verifying Range support.
    This has broken audio scrubbing in at least two previous releases.
    """
    row = storage.get_incident(incident_id)
    if not row or not row.get("snippet_path") or not os.path.exists(row["snippet_path"]):
        raise HTTPException(status_code=404)

    file_path = row["snippet_path"]
    file_size = os.path.getsize(file_path)

    # Common headers for all responses — Accept-Ranges tells the browser
    # that this endpoint supports partial content requests.
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "audio/wav",
    }

    range_header = request.headers.get("range")
    if range_header:
        # Parse "bytes=START-END" (END is optional; omitted = rest of file)
        import re
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1

            # Clamp to file bounds
            start = min(start, file_size - 1)
            end = min(end, file_size - 1)

            content_length = end - start + 1
            with open(file_path, "rb") as f:
                f.seek(start)
                data = f.read(content_length)

            return Response(
                content=data,
                status_code=206,
                headers={
                    **base_headers,
                    "Content-Length": str(content_length),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                },
            )

    # No Range header — return the full file with Accept-Ranges so the browser
    # knows it can make Range requests for subsequent seeks.
    return FileResponse(file_path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})

@app.get("/sw.js")
def service_worker():
    """Serve the Service Worker from root scope so it can intercept /timeline and /snippets/ requests."""
    return FileResponse(os.path.join(static_dir, "sw.js"), media_type="application/javascript")

@app.get("/favicon.ico")
def favicon():
    """Serve favicon from static/ — browsers request this automatically."""
    path = os.path.join(static_dir, "favicon.svg")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/svg+xml")
    raise HTTPException(status_code=404)

@app.get("/.well-known/{rest:path}")
def well_known_sink(rest: str):
    """Catch-all for .well-known requests (e.g., Chrome DevTools probes). Returns
    an empty JSON object instead of a noisy 404 in the logs."""
    return JSONResponse({})

@app.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, view: str = "day"):
    # Always load 30 days — all view switching is client-side (zero extra requests)
    since = (datetime.now().astimezone() - timedelta(days=30)).isoformat()
    rows = storage.list_incidents(limit=5000, since=since)

    # Build client-safe incident list (server-side paths excluded for security)
    incidents = []
    for r in rows:
        incidents.append({
            "id": r["id"],
            "start_ts": r["start_ts"],
            "end_ts": r.get("end_ts"),
            "duration_sec": r.get("duration_sec"),
            "start_db": r.get("start_db"),
            "peak_db": r.get("peak_db"),
            "avg_db": r.get("avg_db"),
            "threshold_db": r.get("threshold_db"),
            "music_like_score": r.get("music_like_score"),
            "classification": r.get("classification", ""),
            "class_journal": r.get("class_journal", ""),
            "mode": r.get("mode", ""),
            "responded": bool(r.get("responded")),
            "notes": r.get("notes", ""),
            "has_snippet": bool(r.get("snippet_path")),
        })

    return templates.TemplateResponse(request, "timeline.html", {
        "view": view,
        "incidents_json": json.dumps(incidents),
        "ordinance_json": json.dumps(ORDINANCE),
        "borderline_margin_db": float(cfg["detection"].get("borderline_margin_db", 5.0)),
    })

@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    return templates.TemplateResponse(request, "config.html", {"raw": raw, "message": request.query_params.get("msg")})

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
    return templates.TemplateResponse(request, "build.html", {"meta": meta, "photo_path": photo_path})

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
    return templates.TemplateResponse(request, "calibration.html", {
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
    storage.add_calibration_profile(name, offset, datetime.now().astimezone().replace(microsecond=0).isoformat())
    return JSONResponse({"offset_db": offset})

@app.post("/calibration/apply")
def calibration_apply(request: Request, offset_db: float = Form(...)):
    """Apply a calibration profile by updating the running config and saving to YAML."""
    must_auth(request)
    cfg["detection"]["calibration_offset_db"] = offset_db

    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        import re
        # Replace the calibration_offset_db value in the raw YAML text
        updated = re.sub(
            r"(calibration_offset_db:\s*)[\d.\-]+",
            rf"\g<1>{offset_db}",
            raw
        )
        save_yaml_text_validated(cfg_path, updated)
    except Exception as e:
        return RedirectResponse(url=f"/calibration?msg=apply-error:{e}", status_code=303)

    return RedirectResponse(url="/calibration?msg=applied", status_code=303)

VALID_SAMPLE_RATES = {22050, 44100, 48000}

@app.post("/calibration/sample-rate")
def set_sample_rate(request: Request, sample_rate: int = Form(...)):
    """Update sample rate in config YAML. Requires a service restart to take effect
    because AudioCapture is initialized once at engine startup."""
    must_auth(request)
    if sample_rate not in VALID_SAMPLE_RATES:
        return RedirectResponse(
            url=f"/calibration?msg=error:invalid sample rate {sample_rate}", status_code=303
        )

    cfg["audio"]["sample_rate"] = sample_rate

    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        import re
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        updated = re.sub(
            r"(sample_rate:\s*)\d+",
            rf"\g<1>{sample_rate}",
            raw
        )
        save_yaml_text_validated(cfg_path, updated)
    except Exception as e:
        return RedirectResponse(url=f"/calibration?msg=apply-error:{e}", status_code=303)

    return RedirectResponse(url="/calibration?msg=sample-rate-updated-restart-required", status_code=303)

@app.post("/calibration/noise-floor")
def set_noise_floor(request: Request, noise_floor_db: float = Form(...)):
    """Update the ambient noise floor gate. Takes effect immediately (no restart needed)
    because the engine reads this from cfg on every loop iteration."""
    must_auth(request)
    if not (0.0 <= noise_floor_db <= 80.0):
        return RedirectResponse(
            url=f"/calibration?msg=error:noise floor must be 0–80 dBA", status_code=303
        )

    cfg["detection"]["noise_floor_db"] = noise_floor_db

    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        import re
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        # If the key already exists in YAML, update it; otherwise insert after "mode:" line
        if "noise_floor_db" in raw:
            updated = re.sub(
                r"(noise_floor_db:\s*)[\d.\-]+",
                rf"\g<1>{noise_floor_db}",
                raw
            )
        else:
            updated = re.sub(
                r"(mode:\s*\S+[^\n]*\n)",
                rf"\g<1>  noise_floor_db: {noise_floor_db}           # Ambient gate — signals below this dBA skip DSP analysis entirely\n",
                raw
            )
        save_yaml_text_validated(cfg_path, updated)
    except Exception as e:
        return RedirectResponse(url=f"/calibration?msg=apply-error:{e}", status_code=303)

    return RedirectResponse(url="/calibration?msg=noise-floor-updated", status_code=303)

@app.post("/calibration/delete")
def calibration_delete(request: Request, profile_id: int = Form(...)):
    """Delete a saved calibration profile by ID."""
    must_auth(request)
    storage.delete_calibration_profile(profile_id)
    return RedirectResponse(url="/calibration?msg=profile-deleted", status_code=303)

@app.post("/control/pause")
def pause(request: Request):
    must_auth(request)
    engine.set_armed(False)
    _persist_armed(False)
    return RedirectResponse(url="/", status_code=303)

@app.get("/thresholds", response_class=HTMLResponse)
def thresholds(request: Request):
    zone = cfg["detection"].get("zone", "residential_agricultural")
    zone_data = ORDINANCE.get(zone, ORDINANCE["residential_agricultural"])
    now = datetime.now()
    rule_name, threshold = applicable_threshold(cfg, now)
    night = is_night(now, cfg["detection"]["night_start_hour"], cfg["detection"]["night_end_hour"])

    # Only categories the engine actually evaluates (continuous + intermittent).
    # Other categories (e.g., commerce_industry_A1) are in the ordinance data for
    # reference but never used in threshold comparisons.
    ACTIVE_CATEGORIES = {"continuous_A2_A3", "intermittent_A2_A3", "impulse_A1_A3"}
    active_thresholds = {k: v for k, v in zone_data.items() if k in ACTIVE_CATEGORIES}

    return templates.TemplateResponse(request, "thresholds.html", {
        "ordinance": ORDINANCE,
        "cfg": cfg,
        "zone_label": zone.replace("_", " ").title(),
        "zone_thresholds": active_thresholds,
        "active_rule": rule_name,
        "active_threshold": threshold,
        "period": "nighttime" if night else "daytime",
    })

@app.post("/control/resume")
def resume(request: Request):
    must_auth(request)
    engine.set_armed(True)
    _persist_armed(True)
    return RedirectResponse(url="/", status_code=303)

@app.post("/control/recording")
def toggle_recording(request: Request, enabled: str = Form(...)):
    """Toggle recording_enabled at runtime without requiring a config file edit or restart."""
    must_auth(request)
    new_val = enabled.lower() == "true"
    cfg["audio"]["recording_enabled"] = new_val
    state.set(recording_enabled=new_val)
    return RedirectResponse(url="/", status_code=303)

VALID_DETECTION_MODES = {"continuous", "intermittent", "continuous_music_focus"}

@app.post("/control/detection-mode")
def set_detection_mode(request: Request, detection_mode: str = Form(...), redirect: str = Form("/")):
    """Switch detection mode at runtime. Takes effect immediately — the engine reads
    cfg['detection']['mode'] on every loop iteration."""
    must_auth(request)
    if detection_mode not in VALID_DETECTION_MODES:
        return RedirectResponse(url=f"{redirect}?msg=error:invalid mode {detection_mode}", status_code=303)
    cfg["detection"]["mode"] = detection_mode

    cfg_path = os.environ.get("NOISE_WARDEN_CONFIG", "/opt/noise-warden/current/config/noise_warden.yaml")
    try:
        import re
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        updated = re.sub(
            r"(mode:\s*)\S+(\s*#.*)?",
            rf"\g<1>{detection_mode}\2",
            raw,
            count=1
        )
        save_yaml_text_validated(cfg_path, updated)
    except Exception as e:
        return RedirectResponse(url=f"{redirect}?msg=mode-apply-error:{e}", status_code=303)

    return RedirectResponse(url=f"{redirect}?msg=mode-set-{detection_mode}", status_code=303)

@app.post("/control/force-incident")
def force_incident(request: Request):
    """Force-start a test incident for verifying recording and playback."""
    must_auth(request)
    engine.force_incident()
    return RedirectResponse(url="/", status_code=303)

@app.post("/control/end-forced-incident")
def end_forced_incident(request: Request):
    """End a force-started test incident."""
    must_auth(request)
    engine.end_forced_incident()
    return RedirectResponse(url="/", status_code=303)

@app.post("/control/restart")
def restart_service(request: Request):
    """Restart the service via self-termination. Under systemd (Restart=always),
    the process comes back automatically. Without systemd, the server just stops
    and the user gets a 'stopped' page with manual restart instructions."""
    must_auth(request)
    under_systemd = _running_under_systemd()
    print(f"[web] Service restart requested — self-terminating in 1.5s (systemd={under_systemd})")

    # Schedule self-termination after a brief delay so the response reaches the client.
    # SIGTERM triggers uvicorn's graceful shutdown.
    def _self_terminate():
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.5, _self_terminate).start()

    if under_systemd:
        return HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="15;url=/">'
            '<title>Restarting\u2026</title>'
            '<link rel="stylesheet" href="/static/style.css">'
            '</head><body>'
            '<div class="wrap"><div class="card">'
            '<h2>Restarting Service\u2026</h2>'
            '<p>The service is shutting down and will restart automatically. '
            'This page will redirect to the dashboard in ~15 seconds.</p>'
            '<p>If it doesn\'t come back, check: '
            '<code>sudo systemctl status noise-warden</code></p>'
            '</div></div></body></html>',
            status_code=200,
        )

    # Not under systemd — no auto-restart. Show a stopped page.
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Server Stopped</title>'
        '<link rel="stylesheet" href="/static/style.css">'
        '</head><body>'
        '<div class="wrap"><div class="card">'
        '<h2>Server Stopped</h2>'
        '<p>The noise-warden process has been shut down. '
        'Since this is not running under systemd, it will <strong>not</strong> restart automatically.</p>'
        '<p>Restart manually from your terminal:</p>'
        '<pre>uvicorn noise_warden.main:app --host 127.0.0.1 --port 8787 --reload</pre>'
        '</div></div></body></html>',
        status_code=200,
    )

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
