"""
Tests for noise_warden.web — FastAPI route tests via Starlette TestClient.

web.py has module-level side effects (loads config, creates Storage, starts
Engine). We handle this by patching the module-level objects after import
and using a TestClient that skips the lifespan (no real engine thread).
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# We need to patch load_yaml BEFORE web.py's module-level code runs.
# If web.py has already been imported by another test, we patch the
# already-bound module attributes directly.


@pytest.fixture
def web_app(base_cfg, tmp_path):
    """
    Provide a FastAPI app with all module-level dependencies replaced
    by test-safe instances. Returns (app, storage, state, engine).
    """
    from noise_warden.storage import Storage
    from noise_warden.state import StateStore

    db_path = str(tmp_path / "web_test.db")
    test_storage = Storage(db_path)
    test_state = StateStore()
    test_engine = MagicMock()
    test_engine.thread = None

    # Write the config to a temp file so /config page and module-level load_yaml can read it
    import yaml
    cfg_path = str(tmp_path / "noise_warden.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(base_cfg, f)

    # Create static dirs and seed the sw.js file for the /sw.js route
    static_dir = str(tmp_path / "static")
    os.makedirs(os.path.join(static_dir, "build"), exist_ok=True)
    import shutil
    real_sw = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
    if os.path.exists(real_sw):
        shutil.copy(real_sw, os.path.join(static_dir, "sw.js"))

    # Set env var BEFORE importing web module, so load_yaml() finds the config
    os.environ["NOISE_WARDEN_CONFIG"] = cfg_path

    import noise_warden.web as web_mod

    # Patch module-level objects
    web_mod.cfg = base_cfg
    web_mod.storage = test_storage
    web_mod.state = test_state
    web_mod.engine = test_engine
    web_mod.static_dir = static_dir

    yield web_mod.app, test_storage, test_state, test_engine

    # Cleanup env var
    os.environ.pop("NOISE_WARDEN_CONFIG", None)


@pytest.fixture
def client(web_app):
    """Provide a Starlette TestClient that skips the lifespan handler."""
    from starlette.testclient import TestClient
    app, _, _, _ = web_app
    # raise_server_exceptions=False so we can test error responses
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def storage(web_app):
    _, s, _, _ = web_app
    return s


@pytest.fixture
def state(web_app):
    _, _, st, _ = web_app
    return st


# ---------------------------------------------------------------------------
# GET pages — should be accessible without auth (LAN-trust model)
# ---------------------------------------------------------------------------

class TestGetPages:

    def test_dashboard_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_incidents_returns_200(self, client):
        resp = client.get("/incidents")
        assert resp.status_code == 200

    def test_timeline_returns_200(self, client):
        resp = client.get("/timeline")
        assert resp.status_code == 200

    def test_config_page_returns_200(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200

    def test_build_page_returns_200(self, client):
        resp = client.get("/build")
        assert resp.status_code == 200

    def test_calibration_page_returns_200(self, client):
        resp = client.get("/calibration")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:

    def test_api_state_returns_json(self, client):
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_db" in data
        assert "armed" in data

    def test_api_health_returns_json(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "ok" in data
        assert "mic_ok" in data


# ---------------------------------------------------------------------------
# POST mutations — should work without auth when token is blank
# ---------------------------------------------------------------------------

class TestPostMutations:

    def test_pause_redirects(self, client):
        resp = client.post("/control/pause", follow_redirects=False)
        assert resp.status_code == 303

    def test_resume_redirects(self, client):
        resp = client.post("/control/resume", follow_redirects=False)
        assert resp.status_code == 303

    def test_create_and_delete_incident(self, client, storage, web_app):
        """Create an incident via storage, then delete via the web route."""
        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": "",
        }
        iid = storage.create_incident(sample)
        assert storage.get_incident(iid) is not None

        resp = client.post(f"/incidents/{iid}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert storage.get_incident(iid) is None  # Soft-deleted

    def test_clear_all_incidents(self, client, storage):
        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": "",
        }
        for _ in range(3):
            storage.create_incident(sample)
        assert storage.count_incidents() == 3

        resp = client.post("/incidents/clear", follow_redirects=False)
        assert resp.status_code == 303
        assert storage.count_incidents() == 0

    def test_save_notes(self, client, storage):
        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": "",
        }
        iid = storage.create_incident(sample)
        resp = client.post(
            f"/incidents/{iid}/notes",
            data={"notes": "Bass was shaking my walls"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert storage.get_incident(iid)["notes"] == "Bass was shaking my walls"


# ---------------------------------------------------------------------------
# Auth enforcement on POST mutations
# ---------------------------------------------------------------------------

class TestAuth:

    def test_post_blocked_with_wrong_token(self, web_app, client):
        _, _, _, _ = web_app  # Trigger fixture
        import noise_warden.web as web_mod
        # Enable auth
        web_mod.cfg["app"]["auth_token"] = "secret-test-token"

        resp = client.post(
            "/control/pause",
            headers={"Authorization": "Bearer wrong-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 401

        # Restore
        web_mod.cfg["app"]["auth_token"] = ""

    def test_post_allowed_with_correct_token(self, web_app, client):
        _, _, _, _ = web_app  # Trigger fixture
        import noise_warden.web as web_mod
        web_mod.cfg["app"]["auth_token"] = "secret-test-token"

        resp = client.post(
            "/control/pause",
            headers={"Authorization": "Bearer secret-test-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        web_mod.cfg["app"]["auth_token"] = ""

    def test_get_pages_accessible_with_auth_enabled(self, web_app, client):
        """GET pages should remain accessible even when auth_token is set (LAN-trust model)."""
        import noise_warden.web as web_mod
        web_mod.cfg["app"]["auth_token"] = "secret-test-token"

        resp = client.get("/")
        assert resp.status_code == 200

        resp = client.get("/incidents")
        assert resp.status_code == 200

        web_mod.cfg["app"]["auth_token"] = ""


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCsvExport:

    def test_export_csv_empty(self, client):
        resp = client.get("/export.csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")

    def test_export_csv_with_data(self, client, storage):
        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": "",
        }
        storage.create_incident(sample)
        resp = client.get("/export.csv")
        assert resp.status_code == 200
        assert "start_ts" in resp.text


# ---------------------------------------------------------------------------
# Snippet endpoint
# ---------------------------------------------------------------------------

class TestSnippetEndpoint:

    def test_missing_snippet_returns_404(self, client):
        resp = client.get("/snippets/9999")
        assert resp.status_code == 404

    def test_snippet_with_valid_file(self, client, storage, tmp_path):
        """If a snippet file exists, it should be served with Accept-Ranges header."""
        snippet_file = tmp_path / "test_snippet.wav"
        snippet_file.write_bytes(b"RIFF fake wav content")

        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": str(snippet_file), "notes": "",
        }
        iid = storage.create_incident(sample)

        resp = client.get(f"/snippets/{iid}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        # Accept-Ranges must be present so browsers know scrubbing is supported
        assert resp.headers.get("accept-ranges") == "bytes"

    def test_snippet_range_request_returns_206(self, client, storage, tmp_path):
        """Range requests must return 206 Partial Content — this is what makes
        <audio> scrubbing work. Without it, browsers treat the audio as a
        non-seekable stream. This has broken in multiple previous releases."""
        snippet_file = tmp_path / "test_range.wav"
        content = b"RIFF" + (b"\x00" * 100) + b"fake wav content for range test"
        snippet_file.write_bytes(content)

        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": str(snippet_file), "notes": "",
        }
        iid = storage.create_incident(sample)

        # Request a specific byte range (like a browser seeking within <audio>)
        resp = client.get(f"/snippets/{iid}", headers={"Range": "bytes=10-49"})
        assert resp.status_code == 206
        assert resp.headers["content-type"] == "audio/wav"
        assert "bytes 10-49/" in resp.headers["content-range"]
        assert int(resp.headers["content-length"]) == 40
        assert resp.content == content[10:50]

    def test_snippet_range_open_ended(self, client, storage, tmp_path):
        """Open-ended Range (bytes=X-) should return from X to end of file."""
        snippet_file = tmp_path / "test_range_open.wav"
        content = b"A" * 200
        snippet_file.write_bytes(content)

        sample = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 72.5, "peak_db": 72.5, "avg_db": 72.5,
            "threshold_db": 65.0, "music_like_score": 0.78,
            "beat_confidence": 0.45, "classification": "music_like",
            "mode": "respond", "responded": 0, "merge_count": 0,
            "snippet_path": str(snippet_file), "notes": "",
        }
        iid = storage.create_incident(sample)

        resp = client.get(f"/snippets/{iid}", headers={"Range": "bytes=150-"})
        assert resp.status_code == 206
        assert int(resp.headers["content-length"]) == 50
        assert f"bytes 150-199/200" in resp.headers["content-range"]


# ---------------------------------------------------------------------------
# Thresholds page
# ---------------------------------------------------------------------------

class TestThresholdsPage:

    def test_thresholds_returns_200(self, client):
        resp = client.get("/thresholds")
        assert resp.status_code == 200

    def test_thresholds_contains_ordinance_data(self, client):
        resp = client.get("/thresholds")
        assert "Pleasant Grove" in resp.text
        assert "Residential Agricultural" in resp.text

    def test_thresholds_shows_active_config(self, client):
        resp = client.get("/thresholds")
        # Should display the active detection mode from config
        assert "continuous_music_focus" in resp.text


# ---------------------------------------------------------------------------
# Recording toggle
# ---------------------------------------------------------------------------

class TestRecordingToggle:

    def test_disable_recording(self, client, web_app):
        _, _, _, _ = web_app
        import noise_warden.web as web_mod
        web_mod.cfg["audio"]["recording_enabled"] = True

        resp = client.post(
            "/control/recording",
            data={"enabled": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert web_mod.cfg["audio"]["recording_enabled"] is False

    def test_enable_recording(self, client, web_app):
        _, _, _, _ = web_app
        import noise_warden.web as web_mod
        web_mod.cfg["audio"]["recording_enabled"] = False

        resp = client.post(
            "/control/recording",
            data={"enabled": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert web_mod.cfg["audio"]["recording_enabled"] is True


# ---------------------------------------------------------------------------
# Calibration apply
# ---------------------------------------------------------------------------

class TestCalibrationApply:

    def test_apply_updates_running_config(self, client, web_app, tmp_path):
        _, _, _, _ = web_app
        import noise_warden.web as web_mod

        # Write a config file so the apply route can update it
        import yaml
        cfg_path = str(tmp_path / "noise_warden.yaml")
        with open(cfg_path, "w") as f:
            yaml.dump(web_mod.cfg, f)
        import os
        os.environ["NOISE_WARDEN_CONFIG"] = cfg_path

        original = web_mod.cfg["detection"]["calibration_offset_db"]
        resp = client.post(
            "/calibration/apply",
            data={"offset_db": "92.5"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert web_mod.cfg["detection"]["calibration_offset_db"] == 92.5

        # Restore
        web_mod.cfg["detection"]["calibration_offset_db"] = original


# ---------------------------------------------------------------------------
# Noise floor gate
# ---------------------------------------------------------------------------

class TestNoiseFloorRoute:

    def test_set_noise_floor_updates_config(self, client, web_app, tmp_path):
        _, _, _, _ = web_app
        import noise_warden.web as web_mod
        import yaml, os

        cfg_path = str(tmp_path / "noise_warden.yaml")
        with open(cfg_path, "w") as f:
            yaml.dump(web_mod.cfg, f)
        os.environ["NOISE_WARDEN_CONFIG"] = cfg_path

        original = web_mod.cfg["detection"].get("noise_floor_db", 50.0)
        resp = client.post(
            "/calibration/noise-floor",
            data={"noise_floor_db": "45.0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert web_mod.cfg["detection"]["noise_floor_db"] == 45.0

        # Restore
        web_mod.cfg["detection"]["noise_floor_db"] = original

    def test_rejects_out_of_range(self, client, web_app):
        _, _, _, _ = web_app
        resp = client.post(
            "/calibration/noise-floor",
            data={"noise_floor_db": "95.0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Should redirect with error message
        assert "error" in resp.headers.get("location", "").lower()


# ---------------------------------------------------------------------------
# Timeline (offline-first visual calendar)
# ---------------------------------------------------------------------------

class TestTimeline:

    def test_timeline_embeds_incident_json(self, client, storage):
        """The timeline page must embed all incident data as JSON for client-side rendering."""
        storage.create_incident({
            "start_ts": "2026-04-01T22:00:00Z", "start_db": 72, "peak_db": 72, "avg_db": 72,
            "threshold_db": 55, "music_like_score": 0.5, "beat_confidence": 0.3,
            "classification": "noise", "mode": "record_only",
        })
        resp = client.get("/timeline")
        assert resp.status_code == 200
        # JSON blob must appear in the rendered HTML
        assert "INCIDENTS" in resp.text
        assert '"start_db": 72' in resp.text or '"start_db":72' in resp.text

    def test_timeline_embeds_ordinance_json(self, client):
        """Ordinance data must be embedded for the popup threshold comparison."""
        resp = client.get("/timeline")
        assert "ORDINANCE" in resp.text
        assert "Pleasant Grove" in resp.text

    def test_timeline_has_snippet_flag(self, client, storage, tmp_path):
        """Incidents with snippet files should have has_snippet=true in the JSON."""
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)
        storage.create_incident({
            "start_ts": "2026-04-01T22:00:00Z", "start_db": 70, "peak_db": 70, "avg_db": 70,
            "threshold_db": 55, "music_like_score": 0.5, "beat_confidence": 0.3,
            "classification": "noise", "mode": "record_only", "snippet_path": str(wav),
        })
        resp = client.get("/timeline")
        assert '"has_snippet": true' in resp.text or '"has_snippet":true' in resp.text

    def test_timeline_view_param_preserved(self, client):
        """The view parameter should be reflected in the rendered JS state."""
        resp = client.get("/timeline?view=week")
        assert resp.status_code == 200
        assert "'week'" in resp.text

    def test_timeline_no_server_paths_leaked(self, client, storage, tmp_path):
        """Server-side snippet_path must NOT appear in the rendered HTML."""
        wav = tmp_path / "secret_path.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 40)
        storage.create_incident({
            "start_ts": "2026-04-01T22:00:00Z", "start_db": 70, "peak_db": 70, "avg_db": 70,
            "threshold_db": 55, "music_like_score": 0.5, "beat_confidence": 0.3,
            "classification": "noise", "mode": "record_only", "snippet_path": str(wav),
        })
        resp = client.get("/timeline")
        assert "secret_path.wav" not in resp.text


# ---------------------------------------------------------------------------
# Service Worker route
# ---------------------------------------------------------------------------

class TestServiceWorker:

    def test_sw_returns_200(self, client):
        resp = client.get("/sw.js")
        assert resp.status_code == 200

    def test_sw_content_type(self, client):
        resp = client.get("/sw.js")
        assert "javascript" in resp.headers.get("content-type", "")

    def test_sw_contains_cache_strategy(self, client):
        resp = client.get("/sw.js")
        assert "noise-warden-cache-v10" in resp.text
