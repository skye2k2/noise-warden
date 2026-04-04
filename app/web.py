import csv, io, shutil
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .config import AppConfig
from .storage import Storage
from .engine import NoiseEngine
from datetime import datetime

def create_app(config_path='config/noise_warden.yaml'):
    cfg = AppConfig.load(config_path); storage = Storage(cfg.paths['db_path']); engine = NoiseEngine(cfg.raw, storage); engine.start()
    app = FastAPI(title='Noise Warden v2.2'); templates = Jinja2Templates(directory='templates')
    app.mount('/static', StaticFiles(directory='static'), name='static')

    @app.get('/', response_class=HTMLResponse)
    def dashboard(request: Request): return templates.TemplateResponse('dashboard.html', {'request':request,'state':engine.state,'incidents':storage.list_incidents()[:20],'cfg':cfg.raw})

    @app.get('/api/state')
    def api_state(): return engine.state

    @app.get('/incidents', response_class=HTMLResponse)
    def incidents_page(request: Request): return templates.TemplateResponse('incidents.html', {'request':request,'incidents':storage.list_incidents()})

    @app.get('/timeline', response_class=HTMLResponse)
    def timeline_page(request: Request): return templates.TemplateResponse('timeline.html', {'request':request,'incidents':storage.list_incidents()})

    @app.get('/thresholds', response_class=HTMLResponse)
    def thresholds_page(request: Request): return templates.TemplateResponse('thresholds.html', {'request':request,'cfg':cfg.raw})

    @app.get('/build', response_class=HTMLResponse)
    def build_page(request: Request):
        notes = Path(cfg.paths['build_notes_path']).read_text(encoding='utf-8') if Path(cfg.paths['build_notes_path']).exists() else ''
        excerpt = Path(cfg.paths['ordinance_excerpt_path']).read_text(encoding='utf-8') if Path(cfg.paths['ordinance_excerpt_path']).exists() else ''
        return templates.TemplateResponse('build.html', {'request':request,'photo_exists':Path(cfg.paths['build_photo_path']).exists(),'notes':notes,'excerpt':excerpt})

    @app.post('/build/upload_photo')
    async def upload_photo(photo: UploadFile = File(...)):
        dst = Path(cfg.paths['build_photo_path']); dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open('wb') as f: shutil.copyfileobj(photo.file, f)
        static_copy = Path('static/build/build_photo.jpg'); static_copy.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, static_copy)
        return RedirectResponse(url='/build', status_code=303)

    @app.post('/build/save_notes')
    async def save_notes(notes: str = Form(...), excerpt: str = Form('')):
        Path(cfg.paths['build_notes_path']).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.paths['build_notes_path']).write_text(notes, encoding='utf-8')
        Path(cfg.paths['ordinance_excerpt_path']).write_text(excerpt, encoding='utf-8')
        return RedirectResponse(url='/build', status_code=303)

    @app.get('/calibration', response_class=HTMLResponse)
    def calibration_page(request: Request): return templates.TemplateResponse('calibration.html', {'request':request,'profiles':storage.list_calibration_profiles(),'cfg':cfg.raw})

    @app.post('/calibration/compute')
    async def calibration_compute(name: str = Form(...), reference_spl_db: float = Form(...), observed_raw_dbfs: float = Form(...)):
        offset = float(reference_spl_db - observed_raw_dbfs)
        storage.add_calibration_profile(name, offset, datetime.now().isoformat(timespec='seconds'))
        return JSONResponse({'offset_db': offset})

    @app.get('/incidents/{incident_id}/audio')
    def incident_audio(incident_id: int):
        inc = storage.get_incident(incident_id)
        if not inc or not inc.get('snippet_path'): return JSONResponse({'error':'No audio'}, status_code=404)
        return FileResponse(inc['snippet_path'], media_type='audio/wav', filename=Path(inc['snippet_path']).name)

    @app.post('/incidents/{incident_id}/delete')
    def delete_incident(incident_id: int): storage.soft_delete_incident(incident_id); return RedirectResponse(url='/incidents', status_code=303)

    @app.post('/incidents/clear')
    def clear_incidents(): storage.clear_incidents(); return RedirectResponse(url='/incidents', status_code=303)

    @app.get('/incidents/export.csv')
    def export_csv():
        incidents = storage.list_incidents(); sio = io.StringIO()
        if incidents:
            writer = csv.DictWriter(sio, fieldnames=list(incidents[0].keys())); writer.writeheader(); writer.writerows(incidents)
        else:
            csv.writer(sio).writerow(['no_data'])
        return StreamingResponse(iter([sio.getvalue()]), media_type='text/csv', headers={'Content-Disposition':'attachment; filename=incidents.csv'})

    @app.get('/config', response_class=HTMLResponse)
    def config_page(request: Request):
        text = Path(config_path).read_text(encoding='utf-8')
        return templates.TemplateResponse('config.html', {'request':request,'config_text':text})
    @app.post('/config/save')
    async def config_save(config_text: str = Form(...)):
        Path(config_path).write_text(config_text, encoding='utf-8')
        return RedirectResponse(url='/config', status_code=303)

    return app
