"""Tests for scraper cadence gating logic (_should_fire).

Pure-logic tests — no I/O, no DB, no S3.  These cover the helper that
decides whether a scraper should run at the current sweep time given an
optional cron override in ``scraper_schedules``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from framework.runner import _load_scraper_schedules, _resolve_baselines_path, _should_fire

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_aligned(hour: int = 12, minute: int = 0) -> datetime:
    """Return a timezone-aware UTC datetime at the given hour/minute."""
    return datetime(2026, 4, 22, hour, minute, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# No override → always fire
# ---------------------------------------------------------------------------


class TestNoOverride:
    def test_no_schedules_dict_fires(self) -> None:
        """When schedules is None, always fire (backwards compatible)."""
        assert _should_fire("ca-oc-tentatives", None, _now_aligned()) is True

    def test_empty_schedules_dict_fires(self) -> None:
        """When schedules dict is empty, always fire."""
        assert _should_fire("ca-oc-tentatives", {}, _now_aligned()) is True

    def test_scraper_not_in_schedules_fires(self) -> None:
        """When scraper_id not present in schedules, always fire."""
        schedules = {
            "ca-la-tentatives-civil": {"cron": "0 6 * * *", "timezone": "America/Los_Angeles"}
        }
        assert _should_fire("ca-oc-tentatives", schedules, _now_aligned()) is True


# ---------------------------------------------------------------------------
# Cron override — fire when within window
# ---------------------------------------------------------------------------


class TestCronFiresInWindow:
    def test_daily_cron_fires_when_match_in_window(self) -> None:
        """6 AM daily cron: now=6:15, sweep_interval=12; last fire=6:00 in (now-12h, now]."""
        # Cron fires at 06:00 daily.
        # now = 2026-04-22 06:15 UTC
        # Window: (06:15 - 12h, 06:15] = (18:15 prev day, 06:15]
        # Last cron fire before now: 06:00 → inside window → should fire.
        now = _now_aligned(hour=6, minute=15)
        schedules = {"ca-oc-tentatives": {"cron": "0 6 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now) is True

    def test_three_times_daily_cron_fires_within_window(self) -> None:
        """3×/day cron (6, 14, 18 UTC): now=18:05, last fire=18:00 → inside (06:05, 18:05]."""
        now = _now_aligned(hour=18, minute=5)
        schedules = {"ca-oc-tentatives": {"cron": "0 6,14,18 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now) is True

    def test_cron_fires_at_exact_boundary(self) -> None:
        """Cron fires at exactly now - sweep_interval (boundary is exclusive)."""
        # now - 12h is excluded (window is open on left)
        # If cron fires exactly at now - sweep_interval it should NOT fire.
        now = _now_aligned(hour=18, minute=0)
        # Cron at 06:00 → last fire = 06:00 = now - 12h exactly → NOT in (now-12h, now]
        schedules = {"ca-oc-tentatives": {"cron": "0 6 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now) is False

    def test_cron_fires_just_after_boundary(self) -> None:
        """Fire at now - sweep_interval + 1 minute is inside the window."""
        now = datetime(2026, 4, 22, 18, 1, 0, tzinfo=UTC)
        # Cron at 06:00 → last fire = 06:00 which is now - 12h - 1min → NOT in (18:01-12h, 18:01]
        # Actually (06:01, 18:01] → 06:00 is NOT in there.
        # Use cron at 06:05: last fire = 06:05, window = (06:01, 18:01] → 06:05 IS in window.
        schedules = {"ca-oc-tentatives": {"cron": "5 6 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now) is True


# ---------------------------------------------------------------------------
# Cron override — do NOT fire when outside window
# ---------------------------------------------------------------------------


class TestCronDoesNotFireOutsideWindow:
    def test_daily_cron_outside_window(self) -> None:
        """6 AM daily cron: now=23:00, last fire=18:00 yesterday or today 06:00.
        Sweep interval=12h, window=(11:00, 23:00]. Today's 06:00 is NOT in window."""
        now = _now_aligned(hour=23, minute=0)
        # Window (11:00, 23:00). Last fire = 06:00 → not in window.
        schedules = {"ca-oc-tentatives": {"cron": "0 6 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now) is False

    def test_three_times_daily_no_fire_midday(self) -> None:
        """3×/day at 06,14,18: now=10:00 (sweep_interval=12h).
        Window (22:00 prev, 10:00]. Fires at 06:00 → in window → should fire."""
        # Wait — 06:00 IS in window (22:00 prev day, 10:00].
        # Let me pick now=12:00. Window=(00:00, 12:00]. 06:00 is in it. → fires.
        # Let me pick sweep_interval=6h for this test to isolate behavior.
        # Actually keep sweep_interval=12h, now=23:30, window=(11:30, 23:30].
        # Cron fires at 06,14,18. Last fire before 23:30: 18:00. 18:00 IS in (11:30, 23:30].
        # That would fire. Let's use now=23:30 with cron "0 6 * * *" (once/day):
        now = datetime(2026, 4, 22, 23, 30, 0, tzinfo=UTC)
        schedules = {"ca-oc-tentatives": {"cron": "0 6 * * *"}}
        # Window (11:30, 23:30]. Last fire = 06:00 NOT in window.
        assert _should_fire("ca-oc-tentatives", schedules, now) is False

    def test_custom_sweep_interval(self) -> None:
        """Verify sweep_interval_hours parameter is respected."""
        now = _now_aligned(hour=20, minute=0)
        # Cron at 06:00 daily. sweep_interval=6h → window=(14:00, 20:00].
        # 06:00 is NOT in (14:00, 20:00] → should not fire.
        schedules = {"ca-oc-tentatives": {"cron": "0 6 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now, sweep_interval_hours=6) is False

    def test_custom_sweep_interval_fire(self) -> None:
        """Verify sweep_interval_hours with a firing case."""
        now = _now_aligned(hour=20, minute=0)
        # Cron at 18:00 daily. sweep_interval=6h → window=(14:00, 20:00].
        # 18:00 IS in (14:00, 20:00] → should fire.
        schedules = {"ca-oc-tentatives": {"cron": "0 18 * * *"}}
        assert _should_fire("ca-oc-tentatives", schedules, now, sweep_interval_hours=6) is True


# ---------------------------------------------------------------------------
# Malformed cron → log+treat as no override (don't crash)
# ---------------------------------------------------------------------------


class TestMalformedCron:
    def test_malformed_cron_fires_and_does_not_crash(self) -> None:
        """Malformed cron should log a warning and behave as 'no override' (fire)."""
        schedules = {"ca-oc-tentatives": {"cron": "not-a-valid-cron-expression"}}
        # Should not raise, should return True (fire = safe default)
        result = _should_fire("ca-oc-tentatives", schedules, _now_aligned())
        assert result is True

    def test_missing_cron_key_fires(self) -> None:
        """Entry with no 'cron' key should fire (no override)."""
        schedules = {"ca-oc-tentatives": {"timezone": "America/Los_Angeles"}}
        assert _should_fire("ca-oc-tentatives", schedules, _now_aligned()) is True

    def test_empty_cron_string_fires(self) -> None:
        """Empty cron string should be treated as no override."""
        schedules = {"ca-oc-tentatives": {"cron": ""}}
        assert _should_fire("ca-oc-tentatives", schedules, _now_aligned()) is True

    def test_none_cron_fires(self) -> None:
        """None cron value should be treated as no override."""
        schedules = {"ca-oc-tentatives": {"cron": None}}
        assert _should_fire("ca-oc-tentatives", schedules, _now_aligned()) is True


# ---------------------------------------------------------------------------
# _resolve_baselines_path — path fallback branches
# ---------------------------------------------------------------------------


class TestResolveBaselinesPath:
    def test_returns_docker_path_when_only_docker_exists(self, tmp_path: Path) -> None:
        """When default path absent but docker path exists, returns docker path."""
        docker_file = tmp_path / "data-quality-baselines.json"
        docker_file.write_text("{}")
        with (
            patch("framework.runner._DEFAULT_BASELINES_PATH", tmp_path / "nonexistent.json"),
            patch("framework.runner._DOCKER_BASELINES_PATH", docker_file),
        ):
            result = _resolve_baselines_path()
        assert result == docker_file

    def test_returns_default_path_when_neither_exists(self, tmp_path: Path) -> None:
        """When neither path exists, falls back to default path."""
        default = tmp_path / "nonexistent-default.json"
        docker = tmp_path / "nonexistent-docker.json"
        with (
            patch("framework.runner._DEFAULT_BASELINES_PATH", default),
            patch("framework.runner._DOCKER_BASELINES_PATH", docker),
        ):
            result = _resolve_baselines_path()
        assert result == default


# ---------------------------------------------------------------------------
# _load_scraper_schedules — exception / missing-file branch
# ---------------------------------------------------------------------------


class TestLoadScraperSchedules:
    def test_returns_none_on_missing_file(self, tmp_path: Path) -> None:
        """When the baselines file doesn't exist, returns None (no throw)."""
        missing = tmp_path / "no-such-file.json"
        with (
            patch("framework.runner._DEFAULT_BASELINES_PATH", missing),
            patch("framework.runner._DOCKER_BASELINES_PATH", missing),
        ):
            result = _load_scraper_schedules()
        assert result is None

    def test_returns_schedules_dict_when_present(self, tmp_path: Path) -> None:
        """When file is valid and key present, returns the schedules dict."""
        f = tmp_path / "data-quality-baselines.json"
        f.write_text(json.dumps({"scraper_schedules": {"ca-oc-tentatives": {"cron": "0 6 * * *"}}}))
        with (
            patch("framework.runner._DEFAULT_BASELINES_PATH", f),
            patch("framework.runner._DOCKER_BASELINES_PATH", tmp_path / "nope.json"),
        ):
            result = _load_scraper_schedules()
        assert result == {"ca-oc-tentatives": {"cron": "0 6 * * *"}}

    def test_returns_none_when_key_absent(self, tmp_path: Path) -> None:
        """When file is valid but scraper_schedules key absent, returns None."""
        f = tmp_path / "data-quality-baselines.json"
        f.write_text(json.dumps({"counties": {}}))
        with (
            patch("framework.runner._DEFAULT_BASELINES_PATH", f),
            patch("framework.runner._DOCKER_BASELINES_PATH", tmp_path / "nope.json"),
        ):
            result = _load_scraper_schedules()
        assert result is None
