"""
Tests for noise_warden.ordinance — threshold lookups and day/night logic.

These protect the core enforcement logic: which thresholds apply at what
time of day, and whether the system correctly identifies day vs. night.
"""
from datetime import datetime

import pytest

from noise_warden.ordinance import ORDINANCE, applicable_threshold, is_night


# ---------------------------------------------------------------------------
# is_night
# ---------------------------------------------------------------------------

class TestIsNight:
    """Boundary tests for the night window (default 22:00–07:00)."""

    def test_midnight_is_night(self):
        assert is_night(datetime(2026, 4, 1, 0, 0)) is True

    def test_3am_is_night(self):
        assert is_night(datetime(2026, 4, 1, 3, 0)) is True

    def test_6am_is_night(self):
        """6 AM is before the default night_end of 7."""
        assert is_night(datetime(2026, 4, 1, 6, 0)) is True

    def test_7am_is_day(self):
        """7 AM is the boundary — not night."""
        assert is_night(datetime(2026, 4, 1, 7, 0)) is False

    def test_noon_is_day(self):
        assert is_night(datetime(2026, 4, 1, 12, 0)) is False

    def test_9pm_is_day(self):
        """21:00 is still day (night starts at 22)."""
        assert is_night(datetime(2026, 4, 1, 21, 0)) is False

    def test_10pm_is_night(self):
        """22:00 is the night boundary — is night."""
        assert is_night(datetime(2026, 4, 1, 22, 0)) is True

    def test_11pm_is_night(self):
        assert is_night(datetime(2026, 4, 1, 23, 0)) is True

    def test_custom_window_early_night(self):
        """Custom night window 20:00–06:00 → 20:00 is night."""
        assert is_night(datetime(2026, 4, 1, 20, 0), night_start=20, night_end=6) is True

    def test_custom_window_still_day(self):
        """Custom night window 20:00–06:00 → 19:00 is still day."""
        assert is_night(datetime(2026, 4, 1, 19, 0), night_start=20, night_end=6) is False


# ---------------------------------------------------------------------------
# applicable_threshold
# ---------------------------------------------------------------------------

class TestApplicableThreshold:
    """Verify that the correct ordinance threshold is returned for each mode/time combo."""

    def _make_cfg(self, mode="continuous_music_focus", zone="residential_agricultural"):
        return {
            "detection": {
                "zone": zone,
                "mode": mode,
                "night_start_hour": 22,
                "night_end_hour": 7,
            }
        }

    # -- Continuous mode (default for music focus) --

    def test_continuous_day(self):
        cfg = self._make_cfg("continuous_music_focus")
        rule, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 14, 0))
        assert rule == "continuous_A2_A3"
        assert threshold == 65.0

    def test_continuous_night(self):
        cfg = self._make_cfg("continuous_music_focus")
        rule, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 23, 0))
        assert rule == "continuous_A2_A3"
        assert threshold == 55.0

    # -- Intermittent mode --

    def test_intermittent_day(self):
        cfg = self._make_cfg("intermittent")
        rule, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 14, 0))
        assert rule == "intermittent_A2_A3"
        assert threshold == 70.0

    def test_intermittent_night(self):
        cfg = self._make_cfg("intermittent")
        rule, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 23, 0))
        assert rule == "intermittent_A2_A3"
        assert threshold == 60.0

    # -- Plain continuous mode (same thresholds as continuous_music_focus) --

    def test_plain_continuous_day(self):
        cfg = self._make_cfg("continuous")
        rule, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 14, 0))
        assert rule == "continuous_A2_A3"
        assert threshold == 65.0

    # -- Night boundary edge cases --

    def test_boundary_7am_gets_day_threshold(self):
        cfg = self._make_cfg("continuous_music_focus")
        _, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 7, 0))
        assert threshold == 65.0

    def test_boundary_10pm_gets_night_threshold(self):
        cfg = self._make_cfg("continuous_music_focus")
        _, threshold = applicable_threshold(cfg, datetime(2026, 4, 1, 22, 0))
        assert threshold == 55.0


# ---------------------------------------------------------------------------
# ORDINANCE data integrity
# ---------------------------------------------------------------------------

class TestOrdinanceData:
    """Validate the static ordinance data structure hasn't been accidentally mangled."""

    def test_city_is_pleasant_grove(self):
        assert ORDINANCE["city"] == "Pleasant Grove, UT"

    def test_residential_thresholds_exist(self):
        zone = ORDINANCE["residential_agricultural"]
        assert "continuous_A2_A3" in zone
        assert "intermittent_A2_A3" in zone
        assert "impulse_A1_A3" in zone

    def test_night_lower_than_day(self):
        """Night thresholds must always be ≤ day thresholds."""
        zone = ORDINANCE["residential_agricultural"]
        for key in zone:
            assert zone[key]["night"] <= zone[key]["day"], f"{key}: night > day"
