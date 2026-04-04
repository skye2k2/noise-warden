from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header, Response
from fastapi.responses import JSONResponse, FileResponse
from app.engine import NoiseWardenEngine


def _auth(engine: NoiseWardenEngine, authorization: str | None):
    if not engine.settings.home_assistant.enabled:
        return
    expected = f"Bearer {engine.settings.home_assistant.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def build_api(engine: NoiseWardenEngine) -> APIRouter:
    r = APIRouter()

    @r.get("/api/status")
    def status():
        return engine.get_status()

    @r.post("/api/arm")
    def arm(authorization: str | None = Header(default=None)):
        _auth(engine, authorization)
        engine.arm()
        return {"ok": True}

    @r.post("/api/disarm")
    def disarm(authorization: str | None = Header(default=None)):
        _auth(engine, authorization)
        engine.disarm()
        return {"ok": True}

    @r.post("/api/kill")
    def kill(authorization: str | None = Header(default=None)):
        _auth(engine, authorization)
        engine.emergency_kill()
        return {"ok": True}

    @r.post("/api/kill/clear")
    def kill_clear(authorization: str | None = Header(default=None)):
        _auth(engine, authorization)
        engine.clear_manual_kill()
        return {"ok": True}

    @r.get("/api/incidents")
    def incidents(limit: int = 200):
        return engine.store.list_incidents(limit=limit)

    @r.delete("/api/incidents")
    def clear_incidents(authorization: str | None = Header(default=None)):
        _auth(engine, authorization)
        engine.store.clear_incidents()
        return {"ok": True}

    @r.get("/api/incidents/export")
    def export_incidents():
        rows = engine.store.list_incidents(limit=5000)
        lines = ["id,started_at,ended_at,day_or_night,peak_db,threshold_db,mode,retaliated,notes_json"]
        for r0 in rows:
            vals = [
                str(r0["id"]),
                r0["started_at"] or "",
                r0["ended_at"] or "",
                r0["day_or_night"] or "",
                str(r0["peak_db"]),
                str(r0["threshold_db"]),
                r0["mode"] or "",
                str(r0["retaliated"]),
                (r0["notes_json"] or "").replace(",", ";"),
            ]
            lines.append(",".join(vals))
        return Response(content="\n".join(lines), media_type="text/csv")

    @r.get("/api/state-log")
    def state_log(limit: int = 200):
        return engine.store.get_state_log(limit=limit)

    return r
