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

    # Create static dirs
    static_dir = str(tmp_path / "static")
    os.makedirs(os.path.join(static_dir, "build"), exist_ok=True)

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
        app, storage, state, engine = web_app
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
        app, storage, state, engine = web_app
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
        """If a snippet file exists, it should be served."""
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
