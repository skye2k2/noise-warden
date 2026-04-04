"""
Tests for noise_warden.state — thread-safe StateStore.

Validates get/set semantics, snapshot isolation, and thread safety.
"""
import threading

from noise_warden.state import StateStore


class TestStateStore:

    def test_initial_defaults(self, tmp_state):
        snap = tmp_state.snapshot()
        assert snap["armed"] is True
        assert snap["running"] is False
        assert snap["mic_ok"] is False
        assert snap["current_db"] == 0.0
        assert snap["mode"] == "idle"
        assert snap["active_incident_id"] is None
        assert snap["last_error"] is None

    def test_set_updates_values(self, tmp_state):
        tmp_state.set(current_db=72.5, mode="incident_active")
        snap = tmp_state.snapshot()
        assert snap["current_db"] == 72.5
        assert snap["mode"] == "incident_active"

    def test_set_updates_timestamp(self, tmp_state):
        snap_before = tmp_state.snapshot()
        tmp_state.set(mic_ok=True)
        snap_after = tmp_state.snapshot()
        # updated_at should change on every set()
        assert snap_after["updated_at"] is not None
        assert snap_after["updated_at"] != snap_before["updated_at"] or snap_before["updated_at"] is None

    def test_snapshot_returns_deep_copy(self, tmp_state):
        """Mutating the snapshot dict must not affect the store's internal state."""
        snap = tmp_state.snapshot()
        snap["current_db"] = 999.0
        snap["mode"] = "HACKED"
        actual = tmp_state.snapshot()
        assert actual["current_db"] == 0.0
        assert actual["mode"] == "idle"

    def test_multiple_sets_accumulate(self, tmp_state):
        tmp_state.set(current_db=55.0)
        tmp_state.set(mic_ok=True)
        tmp_state.set(mode="error")
        snap = tmp_state.snapshot()
        assert snap["current_db"] == 55.0
        assert snap["mic_ok"] is True
        assert snap["mode"] == "error"

    def test_thread_safety_no_crash(self, tmp_state):
        """Hammer set() and snapshot() from multiple threads to detect lock issues."""
        errors = []

        def writer(state, tid):
            try:
                for i in range(200):
                    state.set(current_db=float(tid * 1000 + i))
            except Exception as e:
                errors.append(e)

        def reader(state):
            try:
                for _ in range(200):
                    snap = state.snapshot()
                    # Just verify it's a dict with the expected keys
                    assert "current_db" in snap
            except Exception as e:
                errors.append(e)

        threads = []
        for tid in range(4):
            threads.append(threading.Thread(target=writer, args=(tmp_state, tid)))
        for _ in range(2):
            threads.append(threading.Thread(target=reader, args=(tmp_state,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Thread safety errors: {errors}"
