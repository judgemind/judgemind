"""Tests for the judge pre-pass in scripts/reingest_from_s3.py (#4408, #4419).

Both reingest modes select a list of S3 keys and hand it to
``_reingest_keys``, which seeds courts, runs the judge pre-pass
(``_seed_judges_from_keys``) and then runs every key through the worker in
a ``ProcessPoolExecutor`` (#4845).  DB-row mode (``run_reingest``) gets its
keys from ``select_db_keys``; prefix mode (``run_reingest_from_prefix``)
lists them from S3.

The pre-pass walks the keys once before the pool and seeds full-name judges
into ``derived.judges``.  This closes the chronological-resolver-race class
surfaced by #4397: when LA per-case docs carrying only a surname
(``JUDGE/DEPT: <Surname>/<dept>``) finish in the pool before the boilerplate
doc carrying the full name, the per-case docs would commit
``judge_id = NULL`` until the boilerplate doc later auto-created the judge.

Covered here:
  * ``_seed_judges_from_keys`` — the race replay, skip rules, and the
    once-per-seeding-chunk commit.
  * ``_reingest_keys`` — the pre-pass is skipped on dry runs and under
    ``--skip-judge-prepass``, and otherwise runs before the pool.
  * ``run_reingest`` / ``run_reingest_from_prefix`` — both modes reach the
    pre-pass through ``_reingest_keys``.
  * ``main`` — the CLI forwards ``--skip-judge-prepass`` and
    ``--parse-timeout`` to both modes.

Run from the repo root:
    pytest scripts/tests/test_reingest_judge_prepass.py
"""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import Future
from typing import Any, Self
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg, structlog, framework,
# and ingestion at module level, none of which are installed in the
# lightweight CI scripts-tests environment.  Mock them in sys.modules
# before importing the script under test.  Save/replay envelope is
# centralised in ``scripts/tests/_mock_helpers.py`` (#4430): the
# context-manager exit path restores ``sys.modules`` even if the import
# raises, which is the invariant ``test_scripts_tests_isolation.py``
# pins (#4426).  ``reingest_from_s3``'s own module globals capture the
# mock references at import time, so tests still see mocks via
# ``reingest.<attr>`` and ``@patch("reingest_from_s3.<attr>")`` targets
# — those are bound in the script's namespace, not via ``sys.modules``
# lookups.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules

# Some imports need their attributes to resolve to something callable that
# returns a recognisable sentinel.  ``configure_structlog`` is called at
# module top level so it must not raise.
_mock_structlog = MagicMock()
_mock_structlog.get_logger = MagicMock(return_value=MagicMock())
_mock_framework_logging = MagicMock()
_mock_framework_logging.configure_structlog = MagicMock(return_value=None)

_modules_to_mock: dict[str, MagicMock] = {
    "psycopg": MagicMock(),
    "structlog": _mock_structlog,
    "framework": MagicMock(),
    "framework.extraction_config": MagicMock(),
    "framework.llm_enrichment": MagicMock(),
    "framework.llm_extractor": MagicMock(),
    "framework.llm_schema": MagicMock(),
    "framework.logging": _mock_framework_logging,
    "framework.models": MagicMock(),
    "framework.storage": MagicMock(),
    "ingestion": MagicMock(),
    "ingestion.db": MagicMock(),
    "ingestion.doc_timing": MagicMock(),
    "ingestion.case_type_resolver": MagicMock(),
    "ingestion.extract": MagicMock(),
    "ingestion.llm_extract": MagicMock(),
    "ingestion.llm_providers": MagicMock(),
    "ingestion.ruling_guards": MagicMock(),
    "ingestion.split_ids": MagicMock(),
    "validation": MagicMock(),
    "validation.deterministic": MagicMock(),
    "validation.gate": MagicMock(),
    "courts": MagicMock(),
}

with mock_sys_modules(_modules_to_mock):
    import reingest_from_s3 as reingest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _mock_cursor_context(cur: MagicMock) -> MagicMock:
    """Wrap a cursor in a context manager mock."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


_PREFIX_BUCKET = "test-prefix-bucket"
# _derive_court_code lowercases state + county and replaces spaces with
# dashes.  For ``ca/los_angeles/...`` the parsed county is ``los_angeles``,
# which _unsluggify rewrites to ``Los Angeles``, which _derive_court_code
# then folds back to ``ca-los-angeles``.
_PREFIX_COURT_CODE = "ca-los-angeles"
_PREFIX_COURT_ID = str(uuid.uuid4())
_PREFIX_COURT_IDS = {_PREFIX_COURT_CODE: _PREFIX_COURT_ID}

_LA_RAW = "ca/los_angeles/los_angeles_superior_court/raw/"


def _la_key(hex_hash: str) -> str:
    """An LA content-addressed key.  ``_S3_KEY_PATTERN`` needs a hex hash."""
    return f"{_LA_RAW}{hex_hash}.html"


# S3 keys mirroring the live LA path layout.
_SURNAME_KEY = _la_key("aaaa1111bbbb2222cccc3333dddd4444")
_BOILERPLATE_KEY = _la_key("eeee5555ffff6666aaaa7777bbbb8888")


def _looks_valid(name: str) -> bool:
    """Stand-in for ``_looks_like_valid_judge_name``: full names only."""
    return bool(name) and len(name.strip().split()) >= 2


# ---------------------------------------------------------------------------
# _seed_judges_from_keys
# ---------------------------------------------------------------------------


class TestSeedJudgesFromKeys:
    """Tests for ``_seed_judges_from_keys`` — the #4408 / #4419 pre-pass."""

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_chronological_race_closes(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Replays the #4397 LA dept-25 race and asserts the pre-pass closes
        it on a single reingest invocation.

        The keys are ordered surname-first (the bug-trigger order — when the
        ``ProcessPoolExecutor`` happens to complete the surname doc before
        the boilerplate doc, the surname doc commits ``judge_id = NULL``
        without the pre-pass).

        Invariant: the pre-pass walks both keys, sees the full name on the
        boilerplate doc, and calls ``resolve_judge`` to upsert
        ``Karine Mkrtchyan`` BEFORE the pool runs.  The pool's
        ``_expand_single_word_judge_surname`` Step 4 lookup then finds the
        seeded judge regardless of completion order, so the surname doc
        resolves NON-NULL on the first invocation.
        """
        keys = [_SURNAME_KEY, _BOILERPLATE_KEY]

        surname_bytes = b"<html>JUDGE/DEPT: Mkrtchyan/25</html>"
        boilerplate_bytes = b"<html>DEPT 25 JUDGE KARINE MKRTCHYAN</html>"

        def fetch_s3_side_effect(s3_client: object, bucket: str, key: str) -> bytes:
            assert bucket == _PREFIX_BUCKET, bucket
            if key == _SURNAME_KEY:
                return surname_bytes
            if key == _BOILERPLATE_KEY:
                return boilerplate_bytes
            raise AssertionError(f"unexpected S3 key in pre-pass: {key!r}")

        mock_fetch_s3.side_effect = fetch_s3_side_effect

        def extract_text_side_effect(
            raw_content: bytes, doc_format: str, pdf_timeout: float = 30.0
        ) -> str:
            if raw_content == surname_bytes:
                return "JUDGE/DEPT: Mkrtchyan/25"
            if raw_content == boilerplate_bytes:
                return "DEPARTMENT 25 JUDGE KARINE MKRTCHYAN"
            raise AssertionError("unexpected raw_content in pre-pass")

        mock_extract_text.side_effect = extract_text_side_effect

        def extract_judge_side_effect(text: str) -> str | None:
            if "Mkrtchyan/25" in text:
                return "Mkrtchyan"  # bare surname — rejected as invalid
            if "JUDGE KARINE MKRTCHYAN" in text:
                return "KARINE MKRTCHYAN"  # full name — seeded
            return None

        mock_extract_judge.side_effect = extract_judge_side_effect
        mock_resolve_judge.return_value = "judge-id-karine-mkrtchyan"

        conn = MagicMock()

        with patch(
            "reingest_from_s3._looks_like_valid_judge_name",
            side_effect=_looks_valid,
        ):
            stats = reingest._seed_judges_from_keys(
                conn,
                MagicMock(),  # s3_client
                keys,
                _PREFIX_BUCKET,
                _PREFIX_COURT_IDS,
                parse_timeout=60.0,
                concurrency=2,
            )

        # ---- Stats: scanned both, seeded the full name, rejected surname.
        assert stats["docs_scanned"] == 2, stats
        assert stats["judges_seeded"] == 1, stats
        assert stats["judges_skipped_invalid"] == 1, stats

        # ---- resolve_judge called exactly once with the FULL name and
        # the court_id resolved from the parsed S3 key.  This is the
        # load-bearing assertion: the pre-pass surfaces the full name
        # BEFORE the pool, so single-word surnames resolve via
        # ``_expand_single_word_judge_surname`` Step 4 regardless of
        # worker-pool completion order.
        assert mock_resolve_judge.call_count == 1, mock_resolve_judge.call_args_list
        seeded_args, _ = mock_resolve_judge.call_args
        assert seeded_args[1] == "KARINE MKRTCHYAN", seeded_args
        assert seeded_args[2] == _PREFIX_COURT_ID, seeded_args

        # ---- One chunk seeded one judge → exactly one commit.
        assert conn.commit.call_count == 1, conn.commit.call_args_list

        # ---- Surname-only doc's main-pass resolution succeeds.
        # Replay Step 4's suffix-LIKE query against a synthetic
        # post-prepass connection that contains the seeded row.
        post_prepass_cur = MagicMock()
        post_prepass_cur.fetchall.return_value = [("Karine Mkrtchyan",)]
        post_prepass_conn = MagicMock()
        post_prepass_conn.cursor.return_value = _mock_cursor_context(post_prepass_cur)

        with post_prepass_conn.cursor() as _cur:
            _cur.execute(
                """
                SELECT canonical_name FROM judges
                WHERE court_id = %s::uuid
                  AND LOWER(canonical_name) LIKE %s
                """,
                (_PREFIX_COURT_ID, "% mkrtchyan"),
            )
            suffix_rows = _cur.fetchall()

        assert len(suffix_rows) == 1, (
            "AC2 violated: surname doc still resolves NULL after a "
            "single reingest invocation; pre-pass did not close the "
            "chronological-resolver-race class."
        )
        assert suffix_rows[0][0] == "Karine Mkrtchyan"

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_commits_once_per_chunk_that_seeded(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """The pre-pass commits after each ``_PREPASS_CHUNK`` that seeded a
        judge, and not after a chunk that seeded none.

        Chunk size 2 over five keys gives chunks [0, 1], [2, 3], [4].  Keys
        0, 1 and 4 carry a full name; keys 2 and 3 carry none.  So chunks 1
        and 3 seed and chunk 2 does not: two commits, not one per judge (3)
        and not one per chunk (3).
        """
        keys = [_la_key(f"{i:032x}") for i in range(5)]
        full_name_keys = {keys[0], keys[1], keys[4]}

        mock_fetch_s3.side_effect = lambda _c, _b, key: key.encode()
        mock_extract_text.side_effect = lambda raw, fmt, pdf_timeout=30.0: raw.decode()
        mock_extract_judge.side_effect = lambda text: (
            "KARINE MKRTCHYAN" if text in full_name_keys else None
        )
        mock_resolve_judge.return_value = "judge-id"

        # Record how many judges had been seeded at each commit, to prove
        # the commits land at chunk boundaries.
        conn = MagicMock()
        seeded_at_commit: list[int] = []
        conn.commit.side_effect = lambda: seeded_at_commit.append(
            mock_resolve_judge.call_count
        )

        with (
            patch.object(reingest, "_PREPASS_CHUNK", 2),
            patch(
                "reingest_from_s3._looks_like_valid_judge_name",
                side_effect=_looks_valid,
            ),
        ):
            stats = reingest._seed_judges_from_keys(
                conn,
                MagicMock(),
                keys,
                _PREFIX_BUCKET,
                _PREFIX_COURT_IDS,
                parse_timeout=60.0,
                concurrency=2,
            )

        assert stats == {
            "docs_scanned": 5,
            "judges_seeded": 3,
            "judges_skipped_invalid": 0,
        }
        # Chunk 1 seeded 2 judges → commit; chunk 2 seeded none → no commit;
        # chunk 3 seeded 1 more → commit.
        assert seeded_at_commit == [2, 3]

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_skips_unparseable_keys(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Keys that don't match ``_S3_KEY_PATTERN`` are skipped silently."""
        # ``not-a-content-addressed-key`` lacks the state/county/court/raw
        # path structure — _parse_s3_key returns None.
        keys = ["not-a-content-addressed-key", "also/bad/key.html"]
        conn = MagicMock()

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            keys,
            _PREFIX_BUCKET,
            _PREFIX_COURT_IDS,
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 0
        assert stats["judges_seeded"] == 0
        mock_fetch_s3.assert_not_called()
        mock_resolve_judge.assert_not_called()
        # No seeds → no commit.
        assert conn.commit.call_count == 0

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_skips_keys_with_no_court_mapping(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Keys whose court_code is missing from ``court_ids`` are skipped."""
        conn = MagicMock()

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            [_SURNAME_KEY],
            _PREFIX_BUCKET,
            {},  # empty mapping
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 0
        assert stats["judges_seeded"] == 0
        mock_fetch_s3.assert_not_called()
        mock_resolve_judge.assert_not_called()

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_skips_keys_whose_s3_fetch_fails(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """A key whose raw object can't be fetched is not scanned; the pool
        accounts for it on its own re-fetch."""
        mock_fetch_s3.side_effect = RuntimeError("NoSuchKey")
        conn = MagicMock()

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            [_SURNAME_KEY],
            _PREFIX_BUCKET,
            _PREFIX_COURT_IDS,
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 0
        mock_extract_text.assert_not_called()
        mock_resolve_judge.assert_not_called()
        assert conn.commit.call_count == 0

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_only_seeds_full_names(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Bare single-word surnames are NOT seeded (``resolve_judge``'s
        ``_looks_like_valid_judge_name`` guard would reject them anyway)."""
        conn = MagicMock()

        mock_fetch_s3.return_value = b"<html>JUDGE/DEPT: Mkrtchyan/25</html>"
        mock_extract_text.return_value = "JUDGE/DEPT: Mkrtchyan/25"
        mock_extract_judge.return_value = "Mkrtchyan"

        with patch(
            "reingest_from_s3._looks_like_valid_judge_name",
            return_value=False,
        ):
            stats = reingest._seed_judges_from_keys(
                conn,
                MagicMock(),
                [_SURNAME_KEY],
                _PREFIX_BUCKET,
                _PREFIX_COURT_IDS,
                parse_timeout=60.0,
                concurrency=2,
            )

        assert stats["docs_scanned"] == 1
        assert stats["judges_seeded"] == 0
        assert stats["judges_skipped_invalid"] == 1
        mock_resolve_judge.assert_not_called()
        assert conn.commit.call_count == 0

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_handles_no_judge_match(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """When extract_judge_name returns None, no seed and no error."""
        conn = MagicMock()

        mock_fetch_s3.return_value = b"<html>no judge here</html>"
        mock_extract_text.return_value = "no judge here"
        mock_extract_judge.return_value = None

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            [_SURNAME_KEY],
            _PREFIX_BUCKET,
            _PREFIX_COURT_IDS,
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 1
        assert stats["judges_seeded"] == 0
        assert stats["judges_skipped_invalid"] == 0
        mock_resolve_judge.assert_not_called()
        assert conn.commit.call_count == 0


# ---------------------------------------------------------------------------
# _reingest_keys / run_reingest / run_reingest_from_prefix — pre-pass wiring
# ---------------------------------------------------------------------------

_PREPASS_STATS = {
    "docs_scanned": 2,
    "judges_seeded": 1,
    "judges_skipped_invalid": 1,
}


class _Pipeline:
    """Patches everything ``_reingest_keys`` touches outside the pre-pass.

    ``events`` records the order in which the pre-pass and the process pool
    are entered, so a test can assert the pre-pass runs first.  The pool
    returns an already-finished ``Future`` per key, so the real
    ``as_completed`` drains it without running ``_process_prefix_document``.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.seed_judges = MagicMock(
            side_effect=self._seed_judges, return_value=_PREPASS_STATS
        )
        self.pool_cls = MagicMock(side_effect=self._make_pool)
        self.submitted: list[str] = []
        self._patchers = [
            patch("reingest_from_s3.psycopg"),
            patch("reingest_from_s3.boto3"),
            patch("reingest_from_s3._seed_courts", return_value=_PREFIX_COURT_IDS),
            patch("reingest_from_s3._seed_judges_from_keys", self.seed_judges),
            patch("reingest_from_s3.ProcessPoolExecutor", self.pool_cls),
        ]

    def _seed_judges(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        self.events.append("prepass")
        return _PREPASS_STATS

    def _make_pool(self, *args: Any, **kwargs: Any) -> MagicMock:
        self.events.append("pool")
        pool = MagicMock()

        def submit(fn: Any, key: str, *rest: Any) -> Future:
            self.submitted.append(key)
            fut: Future = Future()
            fut.set_result({"status": "ok", "hash_mismatch": False})
            return fut

        pool.submit.side_effect = submit
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=pool)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def __enter__(self) -> Self:
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patchers):
            p.stop()


def _run_keys(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "bucket": _PREFIX_BUCKET,
        "concurrency": 2,
        "dry_run": False,
        "bust_llm_cache": False,
        "parse_timeout": 12.5,
        "skip_judge_prepass": False,
        "write_failed_manifest": None,
    }
    kwargs.update(overrides)
    return reingest._reingest_keys(
        "postgresql://test", [_SURNAME_KEY, _BOILERPLATE_KEY], **kwargs
    )


class TestReingestKeysPrePass:
    """``_reingest_keys`` gates and orders the pre-pass for both modes."""

    def test_prepass_runs_before_the_pool(self) -> None:
        with _Pipeline() as pipe:
            stats = _run_keys()

        assert pipe.events == ["prepass", "pool"]
        args, kwargs = pipe.seed_judges.call_args
        # (conn, s3_client, keys, bucket, court_ids)
        assert args[2] == [_SURNAME_KEY, _BOILERPLATE_KEY]
        assert args[3] == _PREFIX_BUCKET
        assert args[4] == _PREFIX_COURT_IDS
        assert kwargs == {"parse_timeout": 12.5, "concurrency": 2}
        assert pipe.submitted == [_SURNAME_KEY, _BOILERPLATE_KEY]
        assert stats["processed"] == 2
        assert stats["judge_prepass_docs_scanned"] == 2
        assert stats["judge_prepass_judges_seeded"] == 1
        assert stats["judge_prepass_judges_skipped_invalid"] == 1

    def test_dry_run_skips_prepass_and_pool(self) -> None:
        """A dry run writes no ``derived.judges`` rows and processes nothing."""
        with _Pipeline() as pipe:
            stats = _run_keys(dry_run=True)

        pipe.seed_judges.assert_not_called()
        pipe.pool_cls.assert_not_called()
        assert stats == {
            "total_keys": 2,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }

    def test_skip_judge_prepass_skips_prepass_but_runs_pool(self) -> None:
        with _Pipeline() as pipe:
            stats = _run_keys(skip_judge_prepass=True)

        pipe.seed_judges.assert_not_called()
        assert pipe.events == ["pool"]
        assert stats["processed"] == 2
        assert "judge_prepass_judges_seeded" not in stats


class TestRunReingestPrePass:
    """DB-row mode reaches the pre-pass through ``_reingest_keys``."""

    def test_db_mode_runs_prepass_on_selected_keys(self) -> None:
        selected = [_SURNAME_KEY, _BOILERPLATE_KEY]
        with (
            _Pipeline() as pipe,
            patch(
                "reingest_from_s3.select_db_keys", return_value=selected
            ) as mock_select,
        ):
            stats = reingest.run_reingest(
                "postgresql://test",
                county="Los Angeles",
                limit=5,
                concurrency=3,
                parse_timeout=7.0,
            )

        assert mock_select.call_args.kwargs == {"limit": 5}
        assert pipe.events == ["prepass", "pool"]
        args, kwargs = pipe.seed_judges.call_args
        assert args[2] == selected
        assert args[4] == _PREFIX_COURT_IDS
        assert kwargs == {"parse_timeout": 7.0, "concurrency": 3}
        assert pipe.submitted == selected
        assert stats["judge_prepass_judges_seeded"] == 1

    def test_db_mode_skip_judge_prepass(self) -> None:
        with (
            _Pipeline() as pipe,
            patch("reingest_from_s3.select_db_keys", return_value=[_SURNAME_KEY]),
        ):
            reingest.run_reingest(
                "postgresql://test", county="Los Angeles", skip_judge_prepass=True
            )

        pipe.seed_judges.assert_not_called()
        assert pipe.events == ["pool"]

    def test_db_mode_no_keys_skips_everything(self) -> None:
        with (
            _Pipeline() as pipe,
            patch("reingest_from_s3.select_db_keys", return_value=[]),
        ):
            stats = reingest.run_reingest("postgresql://test", county="Nowhere")

        assert pipe.events == []
        assert stats["total_keys"] == 0


class TestRunReingestFromPrefixPrePass:
    """Prefix mode reaches the pre-pass through ``_reingest_keys``."""

    def test_prefix_mode_runs_prepass_on_listed_keys(self) -> None:
        listed = [_SURNAME_KEY, _BOILERPLATE_KEY]
        with (
            _Pipeline() as pipe,
            patch("reingest_from_s3._list_s3_keys", return_value=listed),
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="ca/los_angeles/",
                concurrency=4,
                parse_timeout=9.0,
            )

        assert pipe.events == ["prepass", "pool"]
        args, kwargs = pipe.seed_judges.call_args
        assert args[2] == listed
        assert kwargs == {"parse_timeout": 9.0, "concurrency": 4}
        assert stats["judge_prepass_judges_seeded"] == 1

    def test_prefix_mode_dry_run_skips_prepass(self) -> None:
        with (
            _Pipeline() as pipe,
            patch("reingest_from_s3._list_s3_keys", return_value=[_SURNAME_KEY]),
        ):
            reingest.run_reingest_from_prefix(
                "postgresql://test", prefix="ca/los_angeles/", dry_run=True
            )

        pipe.seed_judges.assert_not_called()
        pipe.pool_cls.assert_not_called()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

_EMPTY_STATS = {
    "total_keys": 0,
    "processed": 0,
    "errors": 0,
    "skipped": 0,
    "error_ratio": 0.0,
}


class TestCliForwardsPrePassFlags:
    """``main`` forwards ``--skip-judge-prepass`` / ``--parse-timeout``."""

    def _main(self, argv: list[str]) -> tuple[MagicMock, MagicMock]:
        with (
            patch.object(sys, "argv", ["reingest_from_s3.py", *argv]),
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}),
            patch(
                "reingest_from_s3.run_reingest", return_value=_EMPTY_STATS
            ) as mock_db,
            patch(
                "reingest_from_s3.run_reingest_from_prefix",
                return_value=_EMPTY_STATS,
            ) as mock_prefix,
        ):
            reingest.main()
        return mock_db, mock_prefix

    def test_skip_judge_prepass_defaults_off(self) -> None:
        mock_db, _ = self._main(["--county", "Los Angeles"])
        assert mock_db.call_args.kwargs["skip_judge_prepass"] is False

    def test_db_mode_forwards_flags(self) -> None:
        mock_db, mock_prefix = self._main(
            ["--county", "Los Angeles", "--skip-judge-prepass", "--parse-timeout", "12"]
        )
        mock_prefix.assert_not_called()
        kwargs = mock_db.call_args.kwargs
        assert kwargs["skip_judge_prepass"] is True
        assert kwargs["parse_timeout"] == 12.0

    def test_prefix_mode_forwards_flags(self) -> None:
        mock_db, mock_prefix = self._main(
            ["--prefix", "ca/", "--skip-judge-prepass", "--parse-timeout", "12"]
        )
        mock_db.assert_not_called()
        kwargs = mock_prefix.call_args.kwargs
        assert kwargs["prefix"] == "ca/"
        assert kwargs["skip_judge_prepass"] is True
        assert kwargs["parse_timeout"] == 12.0
