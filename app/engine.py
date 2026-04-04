import threading, time, wave, sqlite3
from datetime import datetime
from pathlib import Path
import numpy as np
from .audio import AudioCapture
from .dsp import calibrated_db, spectral_features, music_like_score, beat_confidence, classify_noise
from .response import RelayController, PlaylistPlayer

class NoiseEngine:
    def __init__(self, cfg, storage):
        self.cfg = cfg; self.storage = storage
        a = cfg['audio']
        self.capture = AudioCapture(a['sample_rate'], a['channels'], a['block_sec'], a['prebuffer_sec'])
        self.sample_rate = a['sample_rate']; self.offset = a['calibration_offset_db']
        r = cfg['response']
        self.relay = RelayController(r['relay_gpio_pin']); self.player = PlaylistPlayer(r['playlist_path'], r['player_cmd'])
        self.running = False; self.thread = None; self.active = None; self.recent_db = []
        self.state = {'status':'standby','current_db':0.0,'threshold_db':0.0,'night_mode':False,'current_incident_id':None,'last_update':None,'ha_status':'UNKNOWN'}
    def _is_night(self, now):
        t = self.cfg['thresholds']; h = now.hour
        return h >= t['night_start_hour'] or h < t['night_end_hour']
    def _threshold(self, night):
        t = self.cfg['thresholds']; return t['residential_night_db'] if night else t['residential_day_db']
    def _write_snippet(self, iid, audio):
        path = Path(self.cfg['paths']['snippets_dir']); path.mkdir(parents=True, exist_ok=True)
        fn = path / f'incident_{iid}.wav'
        pcm = np.int16(np.clip(audio, -1, 1) * 32767)
        with wave.open(str(fn), 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(self.sample_rate); wf.writeframes(pcm.tobytes())
        return str(fn)
    def start(self):
        if self.running: return
        self.running = True; self.thread = threading.Thread(target=self.run, daemon=True); self.thread.start()
    def stop(self):
        self.running = False
        if self.thread: self.thread.join(timeout=2)
        self.player.stop(); self.relay.off()
    def run(self):
        audio_cfg = self.cfg['audio']; filters = self.cfg['filters']; detect = self.cfg['detection']; resp = self.cfg['response']
        while self.running:
            now = datetime.now(); night = self._is_night(now); threshold = self._threshold(night)
            block = self.capture.read_block(); db = calibrated_db(block, self.offset)
            feats = spectral_features(block, self.sample_rate); mscore = music_like_score(block, self.sample_rate)
            self.recent_db.append(db); self.recent_db = self.recent_db[-20:]
            bconf = beat_confidence(self.recent_db); prev_db = self.recent_db[-2] if len(self.recent_db) > 1 else db
            classification, reject = classify_noise(db, prev_db, feats, filters)
            self.state.update({'current_db':round(db,2),'threshold_db':threshold,'night_mode':night,'last_update':now.isoformat(timespec='seconds')})
            qualifies = (db >= threshold and not reject and mscore >= detect['min_music_like_score'] and bconf >= detect['min_beat_confidence'])
            if self.active is None:
                if qualifies:
                    mode = 'record_only' if night or not resp['enable_daytime_response'] else 'respond'
                    iid = self.storage.create_incident({
                        'start_ts': now.isoformat(timespec='seconds'),'start_db': db,'peak_db': db,'avg_db': db,'threshold_db': threshold,
                        'music_like_score': mscore,'beat_confidence': bconf,'classification': classification,'mode': mode,
                        'responded': (mode == 'respond'),'merge_count': 0,'snippet_path': None,'notes': None
                    })
                    self.active = {'id':iid,'start':time.time(),'dbs':[db],'audio':[self.capture.get_prebuffer(), block.copy()],'last_above':time.time(),'merge_count':0,'mode':mode}
                    self.state['status']='active'; self.state['current_incident_id']=iid
                    if mode == 'respond': self.relay.on(); self.player.start()
            else:
                self.active['dbs'].append(db); self.active['audio'].append(block.copy())
                if qualifies:
                    self.active['last_above'] = time.time()
                else:
                    gap = time.time() - self.active['last_above']
                    if gap >= audio_cfg['song_gap_merge_sec']:
                        duration = time.time() - self.active['start']
                        if duration >= audio_cfg['min_event_duration_sec']:
                            full_audio = np.concatenate(self.active['audio']) if self.active['audio'] else np.array([], dtype=np.float32)
                            snippet = self._write_snippet(self.active['id'], full_audio)
                            self.storage.update_incident_end(self.active['id'], now.isoformat(timespec='seconds'), duration, max(self.active['dbs']), float(sum(self.active['dbs'])/len(self.active['dbs'])), self.active['merge_count'])
                            with self.storage.conn() as c:
                                c.execute('UPDATE incidents SET snippet_path=? WHERE id=?', (snippet, self.active['id'])); c.commit()
                        if self.active['mode'] == 'respond': self.player.stop(); self.relay.off()
                        self.active = None; self.state['status']='standby'; self.state['current_incident_id']=None
            time.sleep(0.01)
