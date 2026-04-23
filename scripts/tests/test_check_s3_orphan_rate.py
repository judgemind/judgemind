"""Tests for check_s3_orphan_rate script.

Covers:
- FLAT_HASH_RE rejects date-partitioned keys and accepts flat-hash keys (AC #4)
- county_prefix normalises county names correctly
- is_flat_hash is consistent with FLAT_HASH_RE
- Mixed-list filter produces flat-hash-only subset before orphan calculation (AC #4)
- orphan counting returns correct count given a known db-key set
- exit code 1 when rate >= threshold, exit 0 when rate < threshold (AC #2)
- JSON schema fields match the issue example (AC #1)

The script imports boto3 and psycopg at module level; we mock them before
importing to support CI environments without those packages installed.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — inject boto3 + psycopg mocks before the script loads.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "spotcheck"))

_mock_boto3 = MagicMock()
_mock_psycopg = MagicMock()

_modules_to_mock = {
    "boto3": _mock_boto3,
    "psycopg": _mock_psycopg,
}

_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import check_s3_orphan_rate as _script  # noqa: E402

# Restore sys.modules to avoid polluting other test files.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]

FLAT_HASH_RE = _script.FLAT_HASH_RE
county_prefix = _script.county_prefix
is_flat_hash = _script.is_flat_hash
list_flat_hash_keys = _script.list_flat_hash_keys
count_orphans = _script.count_orphans
run_check = _script.run_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_HASH = "a" * 64  # 64 hex chars


def _make_obj(key: str, size: int = 1024) -> dict:
    """Build a minimal list_objects_v2 Contents entry."""
    from datetime import datetime, timezone

    return {
        "Key": key,
        "LastModified": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "Size": size,
    }


# ---------------------------------------------------------------------------
# 1. FLAT_HASH_RE — rejects date-partitioned, accepts flat-hash (AC #4)
# ---------------------------------------------------------------------------


class TestFlatHashRegex:
    """FLAT_HASH_RE must accept flat-hash keys and reject date-partitioned and others."""

    def test_accepts_sc_pdf(self) -> None:
        key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_orange_html(self) -> None:
        key = f"ca/orange/ca-superior/raw/{_VALID_HASH}.html"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_contra_costa_docx(self) -> None:
        key = f"ca/contra_costa/ca-superior/raw/{_VALID_HASH}.docx"
        assert FLAT_HASH_RE.match(key) is not None

    def test_accepts_txt(self) -> None:
        key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.txt"
        assert FLAT_HASH_RE.match(key) is not None

    def test_rejects_date_partitioned_key(self) -> None:
        """Date-partitioned keys (raw/YYYY/MM/DD/…) must NOT match — AC #4."""
        key = "ca/santa_clara/ca-superior/raw/2025/03/14/foo.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_date_partitioned_any_county(self) -> None:
        """Date-partitioned keys rejected regardless of county."""
        key = "ca/orange/ca-superior/raw/2026/01/01/some-file.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_llm_cache_key(self) -> None:
        key = "ca/santa_clara/ca-superior/llm-cache/abcdef.json"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_judge_assignments_key(self) -> None:
        key = "ca/santa_clara/judge-assignments/2026-03.json"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_short_hash(self) -> None:
        short_hash = "a" * 40
        key = f"ca/santa_clara/ca-superior/raw/{short_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_long_hash(self) -> None:
        long_hash = "a" * 65
        key = f"ca/santa_clara/ca-superior/raw/{long_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_non_hex_in_hash(self) -> None:
        bad_hash = "g" * 64
        key = f"ca/santa_clara/ca-superior/raw/{bad_hash}.pdf"
        assert FLAT_HASH_RE.match(key) is None

    def test_rejects_unsupported_extension(self) -> None:
        key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.bin"
        assert FLAT_HASH_RE.match(key) is None


# ---------------------------------------------------------------------------
# 2. county_prefix and is_flat_hash helpers
# ---------------------------------------------------------------------------


class TestCountyPrefix:
    def test_santa_clara_normalised(self) -> None:
        assert county_prefix("Santa Clara") == "ca/santa_clara/"

    def test_orange_normalised(self) -> None:
        assert county_prefix("Orange") == "ca/orange/"

    def test_contra_costa_normalised(self) -> None:
        assert county_prefix("Contra Costa") == "ca/contra_costa/"

    def test_already_lowercase(self) -> None:
        assert county_prefix("los_angeles") == "ca/los_angeles/"


class TestIsFlatHash:
    def test_returns_true_for_valid_key(self) -> None:
        key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        assert is_flat_hash(key) is True

    def test_returns_false_for_date_partitioned(self) -> None:
        key = "ca/santa_clara/ca-superior/raw/2025/03/14/foo.pdf"
        assert is_flat_hash(key) is False

    def test_returns_false_for_random_key(self) -> None:
        assert is_flat_hash("ca/santa_clara/judge-assignments/2026.json") is False


# ---------------------------------------------------------------------------
# 3. list_flat_hash_keys — mixed list filtered to flat-hash only (AC #4)
# ---------------------------------------------------------------------------


class TestListFlatHashKeys:
    def test_mixed_list_filtered_to_flat_hash_only(self) -> None:
        """AC #4: a mixed list of keys is filtered to flat-hash only."""
        flat_key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        date_key = "ca/santa_clara/ca-superior/raw/2025/03/14/foo.pdf"
        judge_key = "ca/santa_clara/judge-assignments/2026.json"

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    _make_obj(flat_key),
                    _make_obj(date_key),
                    _make_obj(judge_key),
                ]
            },
        ]

        result = list_flat_hash_keys(mock_s3, "ca/santa_clara/")
        assert result == [flat_key]
        assert date_key not in result
        assert judge_key not in result

    def test_empty_page_returns_empty_list(self) -> None:
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{}]

        result = list_flat_hash_keys(mock_s3, "ca/santa_clara/")
        assert result == []

    def test_paginator_handles_multiple_pages(self) -> None:
        key1 = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        key2 = f"ca/santa_clara/ca-superior/raw/{'b' * 64}.pdf"

        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(key1)]},
            {"Contents": [_make_obj(key2)]},
        ]

        result = list_flat_hash_keys(mock_s3, "ca/santa_clara/")
        assert sorted(result) == sorted([key1, key2])


# ---------------------------------------------------------------------------
# 4. count_orphans — correct counting given known db-key set
# ---------------------------------------------------------------------------


class TestCountOrphans:
    def _make_conn(self, db_keys: list[str]) -> MagicMock:
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(k,) for k in db_keys]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_no_orphans_when_all_keys_in_db(self) -> None:
        key1 = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        key2 = f"ca/santa_clara/ca-superior/raw/{'b' * 64}.pdf"
        conn = self._make_conn([key1, key2])
        assert count_orphans(conn, [key1, key2]) == 0

    def test_all_orphans_when_none_in_db(self) -> None:
        key1 = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        key2 = f"ca/santa_clara/ca-superior/raw/{'b' * 64}.pdf"
        conn = self._make_conn([])
        assert count_orphans(conn, [key1, key2]) == 2

    def test_partial_orphans(self) -> None:
        """Orphan count is correct when some keys are in DB and some aren't."""
        in_db = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        orphan = f"ca/santa_clara/ca-superior/raw/{'b' * 64}.pdf"
        conn = self._make_conn([in_db])
        assert count_orphans(conn, [in_db, orphan]) == 1

    def test_empty_sampled_keys_returns_zero(self) -> None:
        conn = MagicMock()
        assert count_orphans(conn, []) == 0
        # Should not call DB for empty input.
        conn.cursor.assert_not_called()


# ---------------------------------------------------------------------------
# 5. run_check — exit code 1 when rate >= threshold, 0 otherwise (AC #2)
# ---------------------------------------------------------------------------


class TestRunCheck:
    def _make_s3(self, keys: list[str]) -> MagicMock:
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys]},
        ]
        return mock_s3

    def _make_conn(self, db_keys: list[str]) -> MagicMock:
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(k,) for k in db_keys]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_passed_true_when_rate_below_threshold(self) -> None:
        """AC #2: passed=True when orphan_rate < threshold."""
        # 0 orphans out of 10 → rate=0.0 < 0.05
        keys = [
            f"ca/santa_clara/ca-superior/raw/{'a' * 63}{i:x}.pdf" for i in range(10)
        ]
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)  # all in DB

        result = run_check(s3, conn, county="Santa Clara", n=10, threshold=0.05)
        assert result["passed"] is True
        assert result["orphan_rate"] == 0.0

    def test_passed_false_when_rate_equals_threshold(self) -> None:
        """AC #2: passed=False when orphan_rate == threshold (>= triggers failure)."""
        # 5 orphans out of 100 → rate=0.05 == threshold=0.05
        all_keys = [
            f"ca/santa_clara/ca-superior/raw/{'a' * 63}{i:x}.pdf" for i in range(16)
        ]
        # Use only 10 keys, 5 in DB, 5 not
        in_db = all_keys[:5]
        not_in_db = all_keys[5:10]
        sampled = in_db + not_in_db  # 10 keys, 5 orphans → rate=0.5

        s3 = self._make_s3(sampled)
        conn = self._make_conn(in_db)

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.5)
        assert result["passed"] is False
        assert result["orphan_rate"] == 0.5

    def test_passed_false_when_rate_exceeds_threshold(self) -> None:
        """AC #2: passed=False when orphan_rate > threshold."""
        keys = [
            f"ca/santa_clara/ca-superior/raw/{'a' * 63}{i:x}.pdf" for i in range(10)
        ]
        s3 = self._make_s3(keys)
        conn = self._make_conn([])  # no keys in DB → orphan_rate=1.0

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.05)
        assert result["passed"] is False
        assert result["orphan_rate"] == 1.0

    def test_passed_true_when_rate_strictly_below_threshold(self) -> None:
        """Running with --threshold 0.10 passes when rate is 0.05."""
        keys = [
            f"ca/santa_clara/ca-superior/raw/{'a' * 63}{i:x}.pdf" for i in range(10)
        ]
        # 0 orphans → rate=0.0 < 0.10
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.10)
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# 6. JSON output schema matches issue example (AC #1)
# ---------------------------------------------------------------------------


class TestJsonOutputSchema:
    def _make_s3(self, keys: list[str]) -> MagicMock:
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys]},
        ]
        return mock_s3

    def _make_conn(self, db_keys: list[str]) -> MagicMock:
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(k,) for k in db_keys]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_json_schema_has_all_required_fields(self) -> None:
        """AC #1: JSON output contains county, total_flat_hash, sampled,
        orphan_count, orphan_rate, threshold, passed."""
        keys = [f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"]
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.05)

        required_fields = {
            "county",
            "total_flat_hash",
            "sampled",
            "orphan_count",
            "orphan_rate",
            "threshold",
            "passed",
        }
        assert required_fields == set(result.keys())

    def test_json_schema_types(self) -> None:
        """Field types match the issue example."""
        keys = [f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"]
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.05)

        assert isinstance(result["county"], str)
        assert isinstance(result["total_flat_hash"], int)
        assert isinstance(result["sampled"], int)
        assert isinstance(result["orphan_count"], int)
        assert isinstance(result["orphan_rate"], float)
        assert isinstance(result["threshold"], float)
        assert isinstance(result["passed"], bool)

    def test_json_serialisable(self) -> None:
        """Result must be JSON-serialisable."""
        keys = [f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"]
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)

        result = run_check(s3, conn, county="Santa Clara", n=100, threshold=0.05)
        # Must not raise.
        serialised = json.dumps(result)
        parsed = json.loads(serialised)
        assert parsed["county"] == "santa_clara"

    def test_county_normalised_to_snake_case(self) -> None:
        """county field in output uses snake_case (not the raw input)."""
        keys = [f"ca/contra_costa/ca-superior/raw/{_VALID_HASH}.pdf"]
        s3 = self._make_s3(keys)
        conn = self._make_conn(keys)

        result = run_check(s3, conn, county="Contra Costa", n=100, threshold=0.05)
        assert result["county"] == "contra_costa"


# ---------------------------------------------------------------------------
# 7. main() — exit-code integration (AC #2)
# ---------------------------------------------------------------------------


class TestMainExitCode:
    def _make_s3_client(self, keys: list[str]) -> MagicMock:
        mock_s3_client = MagicMock()
        mock_paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [_make_obj(k) for k in keys]},
        ]
        return mock_s3_client

    def _make_conn(self, db_keys: list[str]) -> MagicMock:
        mock_conn = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [(k,) for k in db_keys]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor_mock)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def test_exit_0_when_healthy(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """main() exits 0 when orphan_rate < threshold."""
        key = f"ca/santa_clara/ca-superior/raw/{_VALID_HASH}.pdf"
        mock_s3_client = self._make_s3_client([key])
        mock_conn = self._make_conn([key])

        with patch("check_s3_orphan_rate.boto3") as mock_boto3_mod:
            with patch("check_s3_orphan_rate.psycopg") as mock_psycopg_mod:
                mock_boto3_mod.client.return_value = mock_s3_client
                mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                    return_value=mock_conn
                )
                mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                    return_value=False
                )

                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                    with patch(
                        "sys.argv",
                        [
                            "check_s3_orphan_rate.py",
                            "--county",
                            "Santa Clara",
                            "--n",
                            "10",
                        ],
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            _script.main()
                        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["passed"] is True

    def test_exit_1_when_above_threshold(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """main() exits 1 when orphan_rate >= threshold."""
        key = f"ca/orange/ca-superior/raw/{_VALID_HASH}.pdf"
        mock_s3_client = self._make_s3_client([key])
        mock_conn = self._make_conn([])  # all orphans

        with patch("check_s3_orphan_rate.boto3") as mock_boto3_mod:
            with patch("check_s3_orphan_rate.psycopg") as mock_psycopg_mod:
                mock_boto3_mod.client.return_value = mock_s3_client
                mock_psycopg_mod.connect.return_value.__enter__ = MagicMock(
                    return_value=mock_conn
                )
                mock_psycopg_mod.connect.return_value.__exit__ = MagicMock(
                    return_value=False
                )

                with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                    with patch(
                        "sys.argv",
                        [
                            "check_s3_orphan_rate.py",
                            "--county",
                            "Orange",
                            "--n",
                            "10",
                            "--threshold",
                            "0.01",
                        ],
                    ):
                        with pytest.raises(SystemExit) as exc_info:
                            _script.main()
                        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["passed"] is False

    def test_missing_database_url_exits_1(self) -> None:
        """main() exits 1 when DATABASE_URL is not set."""
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            with patch("sys.argv", ["check_s3_orphan_rate.py", "--county", "Orange"]):
                with pytest.raises(SystemExit) as exc_info:
                    _script.main()
                assert exc_info.value.code == 1
