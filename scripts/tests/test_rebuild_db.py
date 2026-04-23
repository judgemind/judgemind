"""Tests for the retry-cap gate in rebuild_db.

Covers the pure decision function that determines whether the serial retry
pass should be skipped because the crash rate signals a systemic failure
(connection exhaustion, OOM across many workers) rather than per-document
bad PDFs.

Run with:
    python3 -m pytest scripts/tests/test_rebuild_db.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add scripts/ to path so we can import rebuild_db as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Guard against environment missing scraper-framework venv — rebuild_db
# imports framework.s3_cache at module level, which needs the venv. Tests
# that need the full module import are gated behind this guard.
#
# We split the import test in two: first try to import the module (which
# requires the venv), then import the target function (which requires the
# implementation to exist). This keeps skip messages accurate — "module
# not importable" vs "symbol not yet implemented" are different failure
# modes, though pytest treats both the same.
try:
    import rebuild_db  # type: ignore[import-not-found]

    REBUILD_DB_IMPORTABLE = True
    _should_abort_retry_pass = getattr(rebuild_db, "_should_abort_retry_pass", None)
    _build_abort_log_sample = getattr(rebuild_db, "_build_abort_log_sample", None)
    _default_max_retry_count = getattr(rebuild_db, "_default_max_retry_count", None)
    _default_max_retry_ratio = getattr(rebuild_db, "_default_max_retry_ratio", None)
    FUNC_IMPLEMENTED = _should_abort_retry_pass is not None
    HELPERS_IMPLEMENTED = all(
        [_build_abort_log_sample, _default_max_retry_count, _default_max_retry_ratio]
    )
except ImportError:
    REBUILD_DB_IMPORTABLE = False
    FUNC_IMPLEMENTED = False
    HELPERS_IMPLEMENTED = False
    _should_abort_retry_pass = None
    _build_abort_log_sample = None
    _default_max_retry_count = None
    _default_max_retry_ratio = None


@unittest.skipUnless(
    REBUILD_DB_IMPORTABLE and FUNC_IMPLEMENTED,
    "rebuild_db._should_abort_retry_pass not available (module or symbol missing)",
)
class TestShouldAbortRetryPass(unittest.TestCase):
    """Tests for the _should_abort_retry_pass pure function."""

    def test_no_crashes_does_not_abort(self) -> None:
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=0, total_count=1000, max_count=200, max_ratio=0.10
        )
        self.assertFalse(should_abort)
        self.assertEqual(reason, "")

    def test_small_crash_count_does_not_abort(self) -> None:
        # 5 crashes out of 1000 keys: 0.5% — well below the 10% ratio and
        # the 200 absolute cap.
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=5, total_count=1000, max_count=200, max_ratio=0.10
        )
        self.assertFalse(should_abort)
        self.assertEqual(reason, "")

    def test_absolute_count_exceeded_aborts(self) -> None:
        # 250 crashes: exceeds 200 absolute cap even though 250/10000 is only
        # 2.5% (below the 10% ratio).
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=250, total_count=10000, max_count=200, max_ratio=0.10
        )
        self.assertTrue(should_abort)
        self.assertIn("max-retry-count", reason)

    def test_ratio_exceeded_aborts(self) -> None:
        # 150 crashes out of 1000 keys: 15% — exceeds the 10% ratio even
        # though 150 is below the 200 absolute cap.
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=150, total_count=1000, max_count=200, max_ratio=0.10
        )
        self.assertTrue(should_abort)
        self.assertIn("max-retry-ratio", reason)

    def test_both_thresholds_exceeded_reports_count_first(self) -> None:
        # When both thresholds trip, count is reported first because it is
        # the more actionable signal for operators (it bounds retry time).
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=500, total_count=1000, max_count=200, max_ratio=0.10
        )
        self.assertTrue(should_abort)
        self.assertIn("max-retry-count", reason)

    def test_zero_total_does_not_divide_by_zero(self) -> None:
        # total_count=0 can't happen in practice (we'd exit earlier) but the
        # function must not divide-by-zero if it ever does.
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=0, total_count=0, max_count=200, max_ratio=0.10
        )
        self.assertFalse(should_abort)
        self.assertEqual(reason, "")

    def test_ratio_at_boundary_does_not_abort(self) -> None:
        # Exactly at the ratio threshold: the check is strict ">", so 10%
        # does not abort. This keeps the behavior predictable — operators
        # who set --max-retry-ratio 0.10 mean "up to and including 10%".
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=100, total_count=1000, max_count=200, max_ratio=0.10
        )
        self.assertFalse(should_abort)

    def test_count_at_boundary_does_not_abort(self) -> None:
        # Exactly at the count threshold: the check is strict ">".
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=200, total_count=10000, max_count=200, max_ratio=0.10
        )
        self.assertFalse(should_abort)

    def test_count_above_boundary_aborts(self) -> None:
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=201, total_count=10000, max_count=200, max_ratio=0.10
        )
        self.assertTrue(should_abort)
        self.assertIn("max-retry-count", reason)

    def test_reason_includes_actual_values(self) -> None:
        # Operators reading the error in CloudWatch need to know the values
        # that tripped the gate so they can adjust thresholds if needed.
        should_abort, reason = _should_abort_retry_pass(
            crashed_count=201, total_count=10000, max_count=200, max_ratio=0.10
        )
        self.assertTrue(should_abort)
        self.assertIn("201", reason)
        self.assertIn("200", reason)

    def test_disable_count_cap_with_zero(self) -> None:
        # max_count=0 disables the absolute cap; only ratio applies. This
        # lets operators who trust the ratio bypass the count check.
        should_abort, _ = _should_abort_retry_pass(
            crashed_count=500, total_count=10000, max_count=0, max_ratio=0.10
        )
        # 500/10000 = 5%, under the 10% ratio, and count cap disabled.
        self.assertFalse(should_abort)

    def test_disable_ratio_cap_with_zero(self) -> None:
        # max_ratio=0 disables the ratio cap; only count applies.
        should_abort, _ = _should_abort_retry_pass(
            crashed_count=100, total_count=200, max_count=200, max_ratio=0.0
        )
        # 100/200 = 50%, but ratio disabled and count below cap.
        self.assertFalse(should_abort)


@unittest.skipUnless(
    REBUILD_DB_IMPORTABLE and HELPERS_IMPLEMENTED,
    "rebuild_db retry-cap helpers not available (module or symbols missing)",
)
class TestBuildAbortLogSample(unittest.TestCase):
    """Tests for the _build_abort_log_sample pure function."""

    def test_empty_returns_empty_and_not_truncated(self) -> None:
        sample, truncated = _build_abort_log_sample([])
        self.assertEqual(sample, [])
        self.assertFalse(truncated)

    def test_under_sample_size_returns_all_not_truncated(self) -> None:
        keys = [f"key-{i}" for i in range(5)]
        sample, truncated = _build_abort_log_sample(keys)
        self.assertEqual(sample, keys)
        self.assertFalse(truncated)

    def test_exactly_sample_size_not_truncated(self) -> None:
        # Exactly 20 keys fits in the default sample — no truncation.
        keys = [f"key-{i}" for i in range(20)]
        sample, truncated = _build_abort_log_sample(keys)
        self.assertEqual(len(sample), 20)
        self.assertFalse(truncated)

    def test_above_sample_size_truncates_and_flags(self) -> None:
        keys = [f"key-{i}" for i in range(50)]
        sample, truncated = _build_abort_log_sample(keys)
        self.assertEqual(len(sample), 20)
        self.assertTrue(truncated)
        # The first 20 keys are preserved in order — operators reading
        # the log should see the same slice every time for a given input.
        self.assertEqual(sample, keys[:20])

    def test_custom_sample_size(self) -> None:
        keys = [f"key-{i}" for i in range(10)]
        sample, truncated = _build_abort_log_sample(keys, sample_size=3)
        self.assertEqual(sample, keys[:3])
        self.assertTrue(truncated)


@unittest.skipUnless(
    REBUILD_DB_IMPORTABLE and HELPERS_IMPLEMENTED,
    "rebuild_db retry-cap helpers not available (module or symbols missing)",
)
class TestDefaultMaxRetryCount(unittest.TestCase):
    """Tests for the _default_max_retry_count env-var default resolver."""

    def test_returns_200_when_env_unset(self) -> None:
        # Pass an empty dict so the helper can't fall through to os.environ.
        self.assertEqual(_default_max_retry_count({}), 200)

    def test_reads_from_env(self) -> None:
        self.assertEqual(
            _default_max_retry_count({"REBUILD_MAX_RETRY_COUNT": "500"}), 500
        )

    def test_reads_zero_from_env(self) -> None:
        # Zero disables the cap entirely (see _should_abort_retry_pass).
        self.assertEqual(_default_max_retry_count({"REBUILD_MAX_RETRY_COUNT": "0"}), 0)


@unittest.skipUnless(
    REBUILD_DB_IMPORTABLE and HELPERS_IMPLEMENTED,
    "rebuild_db retry-cap helpers not available (module or symbols missing)",
)
class TestDefaultMaxRetryRatio(unittest.TestCase):
    """Tests for the _default_max_retry_ratio env-var default resolver."""

    def test_returns_0_10_when_env_unset(self) -> None:
        self.assertAlmostEqual(_default_max_retry_ratio({}), 0.10)

    def test_reads_from_env(self) -> None:
        self.assertAlmostEqual(
            _default_max_retry_ratio({"REBUILD_MAX_RETRY_RATIO": "0.25"}), 0.25
        )

    def test_reads_zero_from_env(self) -> None:
        self.assertEqual(
            _default_max_retry_ratio({"REBUILD_MAX_RETRY_RATIO": "0"}), 0.0
        )


if __name__ == "__main__":
    unittest.main()
