"""Deferred-backlog guard for reingest_from_s3.py and rebuild_db.py (#5043).

The May-June 2026 CourtListener raws are a deferred backlog: their reingest
waits on #3954.  The reconciler never re-ingested them (#5036), but a direct
``reingest_from_s3.py`` run did, during #5026: every key failed validation
and the worker replaced each key's rulings with nothing (106 federal rulings
deleted).  These tests pin the shared definition and the default-skip guard
in both scripts.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ingestion import deferred_backlogs as db_mod
from ingestion.deferred_backlogs import (
    DEFERRED_BACKLOGS,
    ONLY_DEFERRED_EXIT_CODE,
    DeferredBacklog,
    log_deferral_split,
    raw_key_scope,
    split_deferred_keys,
)

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

reingest = importlib.import_module("reingest_from_s3")
rebuild_db = importlib.import_module("rebuild_db")

FED = "federal/federal/courtlistener/raw/" + "a" * 64 + ".txt"
FED2 = "federal/federal/courtlistener/raw/" + "b" * 64 + ".txt"
CA = "ca/los_angeles/superior_court/raw/" + "c" * 64 + ".html"
IN_WINDOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
AFTER_WINDOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The shared definition
# ---------------------------------------------------------------------------


def test_one_shared_definition_read_by_reingest_and_rebuild() -> None:
    """The reingest and the rebuild read the shared object (the reconciler's
    side is pinned in ``scripts/tests/test_reconcile_raw_keys.py``)."""
    assert reingest.DEFERRED_BACKLOGS is DEFERRED_BACKLOGS
    assert rebuild_db.DEFERRED_BACKLOGS is DEFERRED_BACKLOGS
    (cl,) = DEFERRED_BACKLOGS
    assert (cl.name, cl.issue, cl.captured_before) == (
        "courtlistener_oom_window",
        3954,
        date(2026, 6, 21),
    )


def test_raw_key_scope() -> None:
    assert raw_key_scope(FED) == "federal/federal"
    assert raw_key_scope(CA) == "ca/los_angeles"
    assert raw_key_scope("tmp/keys/x.txt") is None


def test_matches_key_scope_exclusion_and_cutoff() -> None:
    (cl,) = DEFERRED_BACKLOGS
    assert cl.matches_key("federal/federal", IN_WINDOW)
    assert cl.matches_key("california/statewide", IN_WINDOW)
    assert cl.matches_key("unknown/unknown", IN_WINDOW)
    assert not cl.matches_key("ca/statewide", IN_WINDOW)
    assert not cl.matches_key("ca/los_angeles", IN_WINDOW)
    assert not cl.matches_key("federal/federal", AFTER_WINDOW)
    # A naive timestamp is read as UTC; an unknown capture time is deferred.
    assert cl.matches_key("federal/federal", datetime(2026, 6, 20, 23))
    assert cl.matches_key("federal/federal", None)
    assert DeferredBacklog("x", 1, ("federal/*",)).matches_capture(AFTER_WINDOW)


def test_split_only_reads_capture_time_for_deferred_scopes() -> None:
    seen: list[str] = []

    def capture_time(key: str) -> datetime:
        seen.append(key)
        return IN_WINDOW

    split = split_deferred_keys([CA, FED, "odd-key"], capture_time)
    assert split.kept == [CA, "odd-key"]
    assert split.deferred == {"courtlistener_oom_window": [FED]}
    assert seen == [FED]
    assert split.deferred_count == 1
    assert not split.only_deferred


def test_split_include_deferred_keeps_and_counts() -> None:
    split = split_deferred_keys([FED, CA], lambda _k: IN_WINDOW, include_deferred=True)
    assert split.kept == [FED, CA]
    assert split.deferred_count == 1
    assert not split.only_deferred


def test_split_only_deferred() -> None:
    split = split_deferred_keys([FED, FED2], lambda _k: IN_WINDOW)
    assert split.only_deferred
    assert not split_deferred_keys([], lambda _k: None).only_deferred
    # A later CourtListener capture heals as usual.
    assert split_deferred_keys([FED], lambda _k: AFTER_WINDOW).kept == [FED]


def test_log_deferral_split_skipped_and_included() -> None:
    log = MagicMock()
    log_deferral_split(log, split_deferred_keys([FED], lambda _k: None), mode="x")
    event, kwargs = log.warning.call_args.args[0], log.warning.call_args.kwargs
    assert event == "deferred_backlog_skipped"
    assert kwargs["blocking_issue"] == 3954
    assert kwargs["count"] == 1
    assert kwargs["mode"] == "x"

    log = MagicMock()
    split = split_deferred_keys([FED], lambda _k: None, include_deferred=True)
    log_deferral_split(log, split)
    assert log.error.call_args.args[0] == "deferred_backlog_included"
    assert "#3954" in log.error.call_args.kwargs["warning"]
    log.warning.assert_not_called()


def test_exit_code_is_distinct() -> None:
    assert ONLY_DEFERRED_EXIT_CODE == 5
    assert db_mod.ONLY_DEFERRED_EXIT_CODE == ONLY_DEFERRED_EXIT_CODE


# ---------------------------------------------------------------------------
# reingest_from_s3.py
# ---------------------------------------------------------------------------


def _listing_paginator(objs: list[tuple[str, datetime]]) -> MagicMock:
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": k, "LastModified": lm} for k, lm in objs]}
    ]
    s3 = MagicMock()
    s3.get_paginator.return_value = paginator
    return s3


class TestReingestDeferredBacklog:
    def test_list_s3_keys_records_last_modified(self) -> None:
        s3 = _listing_paginator([(FED, IN_WINDOW), ("federal/x.txt", IN_WINDOW)])
        lm: dict[str, datetime] = {}
        assert reingest._list_s3_keys(s3, "b", "federal/", last_modified=lm) == [FED]
        assert lm == {FED: IN_WINDOW}

    def test_head_last_modified_unreadable_is_none(self) -> None:
        s3 = MagicMock()
        s3.head_object.side_effect = RuntimeError("denied")
        assert reingest._head_last_modified(s3, "b", FED) is None
        s3 = MagicMock()
        s3.head_object.return_value = {"LastModified": IN_WINDOW}
        assert reingest._head_last_modified(s3, "b", FED) == IN_WINDOW

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.boto3")
    def test_prefix_mode_skips_deferred_key_by_default(
        self, mock_boto3: MagicMock, mock_keys: MagicMock
    ) -> None:
        mock_boto3.client.return_value = _listing_paginator(
            [(FED, IN_WINDOW), (FED2, AFTER_WINDOW)]
        )
        mock_keys.return_value = {"total_keys": 1, "processed": 1, "errors": 0, "skipped": 0}
        stats = reingest.run_reingest_from_prefix("pg://x", prefix="federal/")
        assert mock_keys.call_args.args[1] == [FED2]
        assert stats["deferred_skipped"] == 1

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.boto3")
    def test_prefix_mode_include_deferred_processes_it(
        self, mock_boto3: MagicMock, mock_keys: MagicMock
    ) -> None:
        mock_boto3.client.return_value = _listing_paginator([(FED, IN_WINDOW)])
        mock_keys.return_value = {"total_keys": 1, "processed": 1, "errors": 0, "skipped": 0}
        stats = reingest.run_reingest_from_prefix(
            "pg://x", prefix="federal/", include_deferred=True
        )
        assert mock_keys.call_args.args[1] == [FED]
        assert "deferred_skipped" not in stats

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.boto3")
    def test_prefix_mode_only_deferred_runs_nothing(
        self, mock_boto3: MagicMock, mock_keys: MagicMock
    ) -> None:
        mock_boto3.client.return_value = _listing_paginator([(FED, IN_WINDOW)])
        stats = reingest.run_reingest_from_prefix(
            "pg://x", prefix="federal/", s3_key_list=[FED], limit=5
        )
        mock_keys.assert_not_called()
        assert stats["only_deferred"] is True
        assert stats["deferred_skipped"] == 1

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.select_db_keys")
    def test_db_mode_mixed_list_processes_only_non_deferred(
        self,
        mock_select: MagicMock,
        _mock_pg: MagicMock,
        mock_boto3: MagicMock,
        mock_keys: MagicMock,
    ) -> None:
        mock_select.return_value = [FED, CA]
        s3 = MagicMock()
        s3.head_object.return_value = {"LastModified": IN_WINDOW}
        mock_boto3.client.return_value = s3
        mock_keys.return_value = {"total_keys": 1, "processed": 1, "errors": 0, "skipped": 0}
        stats = reingest.run_reingest("pg://x", s3_key_list=[FED, CA], dry_run=True)
        assert mock_keys.call_args.args[1] == [CA]
        assert stats["deferred_skipped"] == 1
        # Only the key in a deferred scope is HEADed.
        s3.head_object.assert_called_once_with(Bucket="judgemind-document-archive-dev", Key=FED)

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.select_db_keys")
    def test_db_mode_include_deferred_and_only_deferred(
        self,
        mock_select: MagicMock,
        _mock_pg: MagicMock,
        mock_boto3: MagicMock,
        mock_keys: MagicMock,
    ) -> None:
        mock_select.return_value = [FED]
        mock_boto3.client.return_value.head_object.return_value = {"LastModified": IN_WINDOW}
        mock_keys.return_value = {"total_keys": 1, "processed": 1, "errors": 0, "skipped": 0}
        reingest.run_reingest("pg://x", include_deferred=True)
        assert mock_keys.call_args.args[1] == [FED]

        mock_keys.reset_mock()
        stats = reingest.run_reingest("pg://x")
        mock_keys.assert_not_called()
        assert stats["only_deferred"] is True

    @patch("reingest_from_s3.run_reingest")
    def test_main_exits_non_zero_when_only_deferred(
        self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "pg://x")
        monkeypatch.setattr(sys, "argv", ["reingest_from_s3.py", "--county", "Federal"])
        mock_run.return_value = {
            "total_keys": 0,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
            "deferred_skipped": 3,
            "only_deferred": True,
        }
        with pytest.raises(SystemExit) as exc:
            reingest.main()
        assert exc.value.code == ONLY_DEFERRED_EXIT_CODE
        assert mock_run.call_args.kwargs["include_deferred"] is False

    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_main_include_deferred_flag_is_forwarded(
        self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "pg://x")
        monkeypatch.setattr(
            sys,
            "argv",
            ["reingest_from_s3.py", "--prefix", "federal/", "--include-deferred"],
        )
        mock_run.return_value = {"total_keys": 1, "processed": 1, "errors": 0, "skipped": 0}
        reingest.main()
        assert mock_run.call_args.kwargs["include_deferred"] is True


# ---------------------------------------------------------------------------
# rebuild_db.py
# ---------------------------------------------------------------------------


class TestRebuildDeferredBacklog:
    def test_list_s3_keys_records_last_modified(self) -> None:
        s3 = _listing_paginator([(FED, IN_WINDOW), (CA, AFTER_WINDOW)])
        lm: dict[str, datetime] = {}
        assert rebuild_db.list_s3_keys(s3, "b", "", last_modified=lm) == [FED, CA]
        assert lm == {FED: IN_WINDOW, CA: AFTER_WINDOW}

    def test_drop_deferred_keys_mixed(self) -> None:
        split = rebuild_db.drop_deferred_keys(
            [FED, FED2, CA],
            include_deferred=False,
            last_modified={FED: IN_WINDOW, FED2: AFTER_WINDOW, CA: IN_WINDOW},
        )
        assert split.kept == [FED2, CA]
        assert split.deferred_count == 1

    def test_drop_deferred_keys_local_cache_is_conservative(self) -> None:
        # No LastModified (local cache): a key in a deferred scope is deferred.
        split = rebuild_db.drop_deferred_keys([FED, CA], include_deferred=False)
        assert split.kept == [CA]
        split = rebuild_db.drop_deferred_keys([FED, CA], include_deferred=True)
        assert split.kept == [FED, CA]

    def test_reset_deferred_backlogs_scope(self) -> None:
        assert rebuild_db.reset_deferred_backlogs("ca", "Los Angeles") == []
        assert rebuild_db.reset_deferred_backlogs("federal", "federal") == list(DEFERRED_BACKLOGS)
        assert rebuild_db.reset_deferred_backlogs("ca", None) == list(DEFERRED_BACKLOGS)

    def test_refuse_deferred_reset(self) -> None:
        rebuild_db.refuse_deferred_reset("ca", "Orange", False)
        rebuild_db.refuse_deferred_reset("federal", "federal", True)
        with pytest.raises(SystemExit) as exc:
            rebuild_db.refuse_deferred_reset("federal", "federal", False)
        assert exc.value.code == ONLY_DEFERRED_EXIT_CODE

    def _run_main(self, tmp_path: Any, keys: list[str], argv: list[str]) -> MagicMock:
        """Run main() on the local-cache path; return the process pool mock."""
        cache = tmp_path / "cache"
        cache.mkdir()
        pool = MagicMock()
        pool.__enter__ = MagicMock(return_value=pool)
        pool.__exit__ = MagicMock(return_value=False)
        future = MagicMock()
        future.result.return_value = {
            "status": "ok",
            "content_format": "html",
            "had_hearing_date": False,
        }
        pool.submit.return_value = future
        with (
            patch("rebuild_db.make_s3_client", return_value=MagicMock()),
            patch("psycopg.connect", return_value=MagicMock()),
            patch("rebuild_db._query_connection_budget", return_value=(0, 0)),
            patch("rebuild_db.list_local_keys", return_value=keys),
            patch("rebuild_db.seed_courts", return_value={}),
            patch("rebuild_db.reset_derived_tables") as global_reset,
            patch("rebuild_db._fetch_rosters"),
            patch("rebuild_db._write_rebuild_marker"),
            patch("concurrent.futures.ProcessPoolExecutor", return_value=pool),
            patch("concurrent.futures.as_completed", return_value=iter([future])),
            patch.dict(
                os.environ,
                {"DATABASE_URL": "postgres://test", "S3_CACHE_DIR": str(cache)},
                clear=False,
            ),
            patch("sys.argv", ["rebuild_db.py", *argv]),
        ):
            self.global_reset = global_reset
            rebuild_db.main()
        return pool

    def test_main_mixed_rebuilds_only_non_deferred(self, tmp_path: Any) -> None:
        pool = self._run_main(tmp_path, [FED, CA], [])
        submitted = [c.args for c in pool.submit.call_args_list]
        assert len(submitted) == 1
        assert CA in submitted[0]
        assert FED not in submitted[0]

    def test_main_include_deferred_rebuilds_both(self, tmp_path: Any) -> None:
        pool = self._run_main(tmp_path, [FED, CA], ["--include-deferred"])
        assert pool.submit.call_count == 2

    def test_main_only_deferred_exits_non_zero(self, tmp_path: Any) -> None:
        with pytest.raises(SystemExit) as exc:
            self._run_main(tmp_path, [FED], ["--state", "federal", "--county", "federal"])
        assert exc.value.code == ONLY_DEFERRED_EXIT_CODE

    def test_main_refuses_global_reset_before_deleting(self, tmp_path: Any) -> None:
        with pytest.raises(SystemExit) as exc:
            self._run_main(tmp_path, [CA], ["--reset"])
        assert exc.value.code == ONLY_DEFERRED_EXIT_CODE
        self.global_reset.assert_not_called()

    def test_main_global_reset_with_include_deferred_runs(self, tmp_path: Any) -> None:
        self._run_main(tmp_path, [CA], ["--reset", "--include-deferred"])
        self.global_reset.assert_called_once()
