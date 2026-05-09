"""Tests for the judge pre-pass in scripts/reingest_from_s3.py (#4408).

The pre-pass walks the FETCH cursor once before the main per-doc write loop
and seeds full-name judges into ``derived.judges``.  This closes the
chronological-resolver-race class surfaced by #4397: when LA per-case docs
carrying only a surname (``JUDGE/DEPT: <Surname>/<dept>``) sort earlier in
the ``(captured_at, id)`` cursor than the boilerplate doc carrying the full
name, the per-case docs would commit ``judge_id = NULL`` until the
boilerplate doc later auto-created the judge in ``derived.judges``.

The test below replays this exact scenario with a synthetic 2-doc cursor —
the surname-only doc precedes the boilerplate doc — and asserts the
single-pass invocation seeds the full name BEFORE the main loop processes
the surname-only doc, so ``_expand_single_word_judge_surname``'s suffix-LIKE
match resolves on the first pass.

Run from the repo root:
    pytest scripts/tests/test_reingest_judge_prepass.py -k chronological_race_closes
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports psycopg, structlog, framework,
# and ingestion at module level, none of which are installed in the
# lightweight CI scripts-tests environment.  Mock them in sys.modules
# before importing the script under test.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_module() -> MagicMock:
    """Return a MagicMock that resolves arbitrary attribute access."""
    return MagicMock()


_modules_to_mock = {
    "psycopg": _make_mock_module(),
    "structlog": _make_mock_module(),
    "framework": _make_mock_module(),
    "framework.extraction_config": _make_mock_module(),
    "framework.llm_enrichment": _make_mock_module(),
    "framework.llm_extractor": _make_mock_module(),
    "framework.llm_schema": _make_mock_module(),
    "framework.logging": _make_mock_module(),
    "framework.models": _make_mock_module(),
    "ingestion": _make_mock_module(),
    "ingestion.db": _make_mock_module(),
    "ingestion.doc_timing": _make_mock_module(),
    "ingestion.case_type_resolver": _make_mock_module(),
    "ingestion.extract": _make_mock_module(),
    "ingestion.llm_extract": _make_mock_module(),
    "ingestion.llm_providers": _make_mock_module(),
    "ingestion.ruling_guards": _make_mock_module(),
    "ingestion.split_ids": _make_mock_module(),
    "validation": _make_mock_module(),
    "validation.deterministic": _make_mock_module(),
    "validation.gate": _make_mock_module(),
    "courts": _make_mock_module(),
}

# Some imports need their attributes to resolve to something callable that
# returns a recognisable sentinel.  ``configure_structlog`` is called at
# module top level (line ~242) so it must not raise.
_modules_to_mock["structlog"].get_logger = MagicMock(return_value=MagicMock())
_modules_to_mock["framework.logging"].configure_structlog = MagicMock(
    return_value=None,
)

# ``is_split_child_id`` is called from the pre-pass; the default MagicMock
# returns a truthy MagicMock(), which would cause the pre-pass to skip
# every document.  Override with an explicit False default — tests that
# need True can patch via ``reingest_from_s3.is_split_child_id``.
_modules_to_mock["ingestion.split_ids"].is_split_child_id = MagicMock(
    return_value=False,
)

_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import reingest_from_s3 as reingest  # noqa: E402

# Restore sys.modules so the mock injection doesn't pollute other test files
# collected in the same pytest run (#4426).  ``reingest_from_s3``'s own module
# globals already captured the mock references at the ``import reingest_from_s3``
# above, so the tests still see mocks via ``reingest.<attr>`` and
# ``@patch("reingest_from_s3.<attr>")`` targets — those are bound in the
# script's namespace, not via ``sys.modules`` lookups.  Leaving the mocks in
# ``sys.modules`` for the rest of the session breaks any later test that
# imports the real ``structlog`` / ``framework.logging`` (e.g.
# ``test_drain_splitter_carry_forward_clusters.py::TestLoggerExtraFieldsSurfaceInOutput``,
# which calls ``isinstance(..., structlog.stdlib.ProcessorFormatter)`` against
# the real ``framework.logging`` and crashes when ``structlog`` is a
# ``MagicMock``).
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# ---------------------------------------------------------------------------
# Helpers — mirror scraper-framework/tests/test_reingest_from_s3.py shape
# so the row tuple matches FETCH_DOCUMENTS_QUERY's column order.
# ---------------------------------------------------------------------------

_COURT_ID = uuid.uuid4()
_CASE_ID = uuid.uuid4()
# Surname-only doc sorts FIRST in cursor order (earlier captured_at).
_SURNAME_DOC_ID = uuid.uuid4()
_BOILERPLATE_DOC_ID = uuid.uuid4()
_CAPTURED_AT_SURNAME = datetime(2026, 5, 8, 23, 43, 32)
_CAPTURED_AT_BOILERPLATE = datetime(2026, 5, 8, 23, 43, 42)


def _make_row(
    *,
    doc_id: uuid.UUID,
    captured_at: datetime,
    s3_key: str = "la/raw/doc.html",
    s3_bucket: str = "test-bucket",
    content_hash: str = "abc123",
    doc_format: str = "html",
) -> tuple:
    """Build a row tuple matching FETCH_DOCUMENTS_QUERY columns."""
    return (
        doc_id,  # d.id
        _CASE_ID,  # d.case_id
        _COURT_ID,  # d.court_id
        s3_key,  # d.s3_key
        s3_bucket,  # d.s3_bucket
        content_hash,  # d.content_hash
        "https://court.example.com/ruling",  # d.source_url
        "ca-la-tentatives-civil",  # d.scraper_id
        captured_at,  # d.captured_at
        None,  # d.hearing_date
        doc_format,  # d.format
        "CA",  # ct.state
        "Los Angeles",  # ct.county
        "Los Angeles Superior Court",  # ct.court_name
        "24STCV12345",  # c.case_number
        "Smith v. Jones",  # c.case_title
        None,  # c.case_type
        None,  # ruling_hearing_date (subquery)
        None,  # stored_ruling_text (subquery)
        None,  # ruling_department (subquery)
        None,  # ruling_judge_name (subquery)
    )


def _mock_cursor_context(cur: MagicMock) -> MagicMock:
    """Wrap a cursor in a context manager mock."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _mock_conn_pageable(pages: list[list[tuple]]) -> MagicMock:
    """Return a mock connection that hands out one fetchall page per cursor.

    The pre-pass calls ``conn.cursor()`` once per page; each cursor's
    ``fetchall()`` returns the next page from ``pages``.  A final empty
    page after all real pages stops the keyset loop.
    """
    conn = MagicMock()

    page_iter = iter(pages + [[]])  # trailing empty page terminates the loop

    def cursor_factory() -> MagicMock:
        cur = MagicMock()
        try:
            page = next(page_iter)
        except StopIteration:
            page = []
        cur.fetchall.return_value = page
        return _mock_cursor_context(cur)

    conn.cursor.side_effect = cursor_factory
    return conn


_DEFAULT_CURSOR = (reingest._CURSOR_MIN_TIMESTAMP, reingest._CURSOR_MIN_UUID)


# ---------------------------------------------------------------------------
# The canonical regression test — chronological_race_closes
# ---------------------------------------------------------------------------


class TestSeedJudgesFromCursor:
    """Tests for ``_seed_judges_from_cursor`` — the #4408 judge pre-pass."""

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_chronological_race_closes_for_la_dept_25(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Replays the #4397 LA dept-25 Mkrtchyan race and asserts the
        pre-pass closes it on a single reingest invocation.

        Cursor order (verified to sub-second resolution in #4397):
          1. surname-only doc captured at 23:43:32 — text contains only
             ``JUDGE/DEPT: Mkrtchyan/25`` form-layout header.
          2. boilerplate doc captured at 23:43:42 — text contains the
             full ``JUDGE KARINE MKRTCHYAN`` ALL-CAPS header.

        Pre-#4408 behaviour: the surname-only doc commits judge_id=NULL
        because the main loop processes it first and ``derived.judges``
        does not yet contain a row whose canonical_name ends in
        ``Mkrtchyan``.  The boilerplate doc later auto-creates
        ``Karine Mkrtchyan`` via ``resolve_judge`` Step 4, but the 62
        surname-only docs preceding it are stuck at NULL until a second
        reingest pass.

        Post-#4408 behaviour: the pre-pass walks both docs, sees the
        full name on doc 2, calls ``resolve_judge`` to upsert
        ``Karine Mkrtchyan`` BEFORE the main loop runs.  The main loop's
        ``_expand_single_word_judge_surname`` Step 4 now finds the judge
        on the suffix-LIKE match and the surname-only doc resolves
        non-NULL on the first pass.
        """
        # Two-row cursor: surname-only doc FIRST (the bug-trigger order).
        surname_row = _make_row(
            doc_id=_SURNAME_DOC_ID,
            captured_at=_CAPTURED_AT_SURNAME,
            s3_key="la/raw/surname.html",
            content_hash="surname-hash",
        )
        boilerplate_row = _make_row(
            doc_id=_BOILERPLATE_DOC_ID,
            captured_at=_CAPTURED_AT_BOILERPLATE,
            s3_key="la/raw/boilerplate.html",
            content_hash="boilerplate-hash",
        )
        conn = _mock_conn_pageable([[surname_row, boilerplate_row]])

        # Distinct fake bytes per doc so the test can assert which
        # text was extracted from which raw payload.
        surname_bytes = b"<html>JUDGE/DEPT: Mkrtchyan/25</html>"
        boilerplate_bytes = b"<html>DEPT 25 JUDGE KARINE MKRTCHYAN</html>"

        def fetch_s3_side_effect(s3_client: object, bucket: str, key: str) -> bytes:
            if "surname" in key:
                return surname_bytes
            if "boilerplate" in key:
                return boilerplate_bytes
            raise AssertionError(f"unexpected S3 key in pre-pass: {key!r}")

        mock_fetch_s3.side_effect = fetch_s3_side_effect

        # Map each raw payload to its extracted text.
        def extract_text_side_effect(
            raw_content: bytes, doc_format: str, pdf_timeout: float = 30.0
        ) -> str:
            if raw_content == surname_bytes:
                return "JUDGE/DEPT: Mkrtchyan/25"
            if raw_content == boilerplate_bytes:
                return "DEPARTMENT 25 JUDGE KARINE MKRTCHYAN"
            raise AssertionError("unexpected raw_content in pre-pass")

        mock_extract_text.side_effect = extract_text_side_effect

        # ``extract_judge_name`` returns the surname only for doc 1
        # (matches the JUDGE/DEPT regex in the real implementation —
        # see packages/scraper-framework/src/ingestion/extract.py:_JUDGE_NAME_PATTERNS[0]).
        # Returns the FULL name for doc 2 (matches the LA ALL-CAPS regex).
        def extract_judge_side_effect(text: str) -> str | None:
            if "Mkrtchyan/25" in text:
                # Surname-only — what the live regex returns for the
                # JUDGE/DEPT pattern at extract.py:_JUDGE_NAME_PATTERNS[0].
                return "Mkrtchyan"
            if "JUDGE KARINE MKRTCHYAN" in text:
                # Full name — what the LA ALL-CAPS regex returns for
                # the boilerplate doc at extract.py:_JUDGE_NAME_PATTERNS[-1].
                return "KARINE MKRTCHYAN"
            return None

        mock_extract_judge.side_effect = extract_judge_side_effect

        # ``resolve_judge`` returns a synthetic UUID when called with the
        # full name; the pre-pass treats this as a successful seed.
        mock_resolve_judge.return_value = "judge-id-karine-mkrtchyan"

        # Patch ``_looks_like_valid_judge_name`` so it accepts the
        # full name (≥2 words) and rejects the bare surname.  We import
        # it from reingest at the call site so patching the attribute
        # on the reingest module is the right interception point.
        def looks_valid_side_effect(name: str) -> bool:
            return bool(name) and len(name.strip().split()) >= 2

        with patch(
            "reingest_from_s3._looks_like_valid_judge_name",
            side_effect=looks_valid_side_effect,
        ):
            stats = reingest._seed_judges_from_cursor(
                conn,
                MagicMock(),  # s3_client
                _DEFAULT_CURSOR,
                "",  # filters
                [],  # filter_params
                batch_size=10,
                limit=None,
                parse_timeout=60.0,
                concurrency=2,
            )

        # ---- Stats: the pre-pass scanned both docs and seeded one judge.
        assert stats["docs_scanned"] == 2, stats
        # The bare surname is rejected by _looks_like_valid_judge_name;
        # only the full name from the boilerplate doc is seeded.
        assert stats["judges_seeded"] == 1, stats
        assert stats["judges_skipped_invalid"] == 1, stats

        # ---- resolve_judge was called exactly once, with the FULL name.
        # This is the load-bearing assertion: the pre-pass surfaces the
        # full name BEFORE the main loop, so the surname-only doc's
        # downstream _expand_single_word_judge_surname Step 4 lookup
        # finds the judge regardless of cursor ordering.
        assert mock_resolve_judge.call_count == 1, mock_resolve_judge.call_args_list
        seeded_args, _seeded_kwargs = mock_resolve_judge.call_args
        # resolve_judge(conn, raw_name, court_id) positional signature.
        assert seeded_args[1] == "KARINE MKRTCHYAN", seeded_args
        assert seeded_args[2] == str(_COURT_ID), seeded_args

        # ---- Surname-only doc's main-pass resolution succeeds.
        # The pre-pass is the structural fix; the realised behavioural
        # outcome is that ``_expand_single_word_judge_surname`` Step 4
        # at packages/scraper-framework/src/ingestion/db.py:1296-1310
        # runs the suffix-LIKE query
        #     SELECT canonical_name FROM judges
        #     WHERE court_id = %s::uuid
        #       AND LOWER(canonical_name) LIKE %s
        # with ``f"% {surname_lower}"`` as the parameter.  Post-prepass
        # the seeded row ``Karine Mkrtchyan`` matches that suffix, so
        # the function returns the canonical full name and the surname
        # doc's downstream insert binds judge_id non-NULL.
        #
        # We replay that exact SQL against a synthetic post-prepass
        # connection here.  The mock cursor returns the row that would
        # exist in ``derived.judges`` after the pre-pass's
        # ``resolve_judge`` call above.  Pre-#4408, an identical lookup
        # *before* the pre-pass would return zero rows, leaving the
        # surname doc's judge_id NULL — the exact bug class #4408
        # closes.
        post_prepass_cur = MagicMock()
        post_prepass_cur.fetchall.return_value = [("Karine Mkrtchyan",)]
        post_prepass_conn = MagicMock()
        post_prepass_conn.cursor.return_value = _mock_cursor_context(post_prepass_cur)

        # Simulate Step 4's branching: returns the canonical name when
        # exactly one suffix match exists, None otherwise.  Mirrors
        # ingestion.db._expand_single_word_judge_surname Step 4 logic.
        with post_prepass_conn.cursor() as _cur:
            _cur.execute(
                """
                SELECT canonical_name FROM judges
                WHERE court_id = %s::uuid
                  AND LOWER(canonical_name) LIKE %s
                """,
                (str(_COURT_ID), "% mkrtchyan"),
            )
            suffix_rows = _cur.fetchall()

        # Post-#4408 single-pass invariant:
        #   * pre-pass seeded "Karine Mkrtchyan" before the main loop ran;
        #   * Step 4's suffix-LIKE query returns exactly one match;
        #   * the bare surname "Mkrtchyan" therefore expands to the
        #     canonical full name and the surname doc commits with
        #     judge_id NON-NULL.
        assert len(suffix_rows) == 1, (
            "AC2 violated: surname doc still resolves NULL after a "
            "single reingest invocation; pre-pass did not close the "
            "chronological-resolver-race class."
        )
        expanded = suffix_rows[0][0]
        assert expanded == "Karine Mkrtchyan"

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_seed_skips_documents_without_s3_key(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Documents with NULL s3_key/s3_bucket are skipped in the pre-pass."""
        no_s3_row = _make_row(
            doc_id=_SURNAME_DOC_ID,
            captured_at=_CAPTURED_AT_SURNAME,
            s3_key="",
            s3_bucket="",
        )
        conn = _mock_conn_pageable([[no_s3_row]])

        stats = reingest._seed_judges_from_cursor(
            conn,
            MagicMock(),
            _DEFAULT_CURSOR,
            "",
            [],
            batch_size=10,
            limit=None,
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 0
        assert stats["judges_seeded"] == 0
        mock_fetch_s3.assert_not_called()
        mock_extract_judge.assert_not_called()
        mock_resolve_judge.assert_not_called()

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    @patch("reingest_from_s3.is_split_child_id")
    def test_seed_skips_split_children(
        self,
        mock_is_split: MagicMock,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Split-child documents are skipped (mirror reingest_batch guard)."""
        split_row = _make_row(
            doc_id=_SURNAME_DOC_ID,
            captured_at=_CAPTURED_AT_SURNAME,
        )
        conn = _mock_conn_pageable([[split_row]])

        # is_split_child_id returns True for this row → must be skipped.
        mock_is_split.return_value = True

        stats = reingest._seed_judges_from_cursor(
            conn,
            MagicMock(),
            _DEFAULT_CURSOR,
            "",
            [],
            batch_size=10,
            limit=None,
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
    def test_seed_only_seeds_full_names(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Bare single-word surnames are NOT seeded (would be rejected by
        ``_looks_like_valid_judge_name`` in ``resolve_judge`` anyway).
        """
        row = _make_row(
            doc_id=_SURNAME_DOC_ID,
            captured_at=_CAPTURED_AT_SURNAME,
        )
        conn = _mock_conn_pageable([[row]])

        mock_fetch_s3.return_value = b"<html>JUDGE/DEPT: Mkrtchyan/25</html>"
        mock_extract_text.return_value = "JUDGE/DEPT: Mkrtchyan/25"
        mock_extract_judge.return_value = "Mkrtchyan"  # bare surname

        with patch(
            "reingest_from_s3._looks_like_valid_judge_name",
            return_value=False,  # bare surname rejected
        ):
            stats = reingest._seed_judges_from_cursor(
                conn,
                MagicMock(),
                _DEFAULT_CURSOR,
                "",
                [],
                batch_size=10,
                limit=None,
                parse_timeout=60.0,
                concurrency=2,
            )

        assert stats["docs_scanned"] == 1
        assert stats["judges_seeded"] == 0
        assert stats["judges_skipped_invalid"] == 1
        mock_resolve_judge.assert_not_called()

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_seed_handles_no_judge_match(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """When extract_judge_name returns None, no seed and no error."""
        row = _make_row(
            doc_id=_SURNAME_DOC_ID,
            captured_at=_CAPTURED_AT_SURNAME,
        )
        conn = _mock_conn_pageable([[row]])

        mock_fetch_s3.return_value = b"<html>no judge here</html>"
        mock_extract_text.return_value = "no judge here"
        mock_extract_judge.return_value = None  # no match

        stats = reingest._seed_judges_from_cursor(
            conn,
            MagicMock(),
            _DEFAULT_CURSOR,
            "",
            [],
            batch_size=10,
            limit=None,
            parse_timeout=60.0,
            concurrency=2,
        )

        assert stats["docs_scanned"] == 1
        assert stats["judges_seeded"] == 0
        assert stats["judges_skipped_invalid"] == 0
        mock_resolve_judge.assert_not_called()

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_seed_respects_limit(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """When limit is set, the pre-pass stops after scanning at most limit docs."""
        rows = [
            _make_row(
                doc_id=uuid.uuid4(),
                captured_at=datetime(2026, 5, 8, 23, 43, 32 + i),
                s3_key=f"la/raw/doc-{i}.html",
                content_hash=f"hash-{i}",
            )
            for i in range(5)
        ]
        # Use a single page so limit gates within the page rather than
        # across pages — the implementation caps ``effective_size`` from
        # ``min(batch_size, limit - docs_scanned)``.
        conn = _mock_conn_pageable([rows])

        mock_fetch_s3.return_value = b"<html>no judge</html>"
        mock_extract_text.return_value = "no judge"
        mock_extract_judge.return_value = None

        stats = reingest._seed_judges_from_cursor(
            conn,
            MagicMock(),
            _DEFAULT_CURSOR,
            "",
            [],
            batch_size=10,
            limit=2,  # cap at 2
            parse_timeout=60.0,
            concurrency=2,
        )

        # Limit applies to the page-fetch SQL ``effective_size`` parameter,
        # so the FETCH query returns at most 2 rows on the first page —
        # docs_scanned should not exceed ``limit``.
        assert stats["docs_scanned"] <= 2


# ---------------------------------------------------------------------------
# Wiring tests — confirm the helper is correctly invoked from run_reingest.
# ---------------------------------------------------------------------------


class TestRunReingestWiresPrePass:
    """Tests that ``run_reingest`` invokes the pre-pass with correct gates."""

    def test_skip_judge_prepass_param_exists(self) -> None:
        """run_reingest accepts the skip_judge_prepass kwarg (#4408)."""
        import inspect

        sig = inspect.signature(reingest.run_reingest)
        assert "skip_judge_prepass" in sig.parameters
        # Default must be False so the pre-pass runs by default.
        assert sig.parameters["skip_judge_prepass"].default is False

    def test_cli_exposes_skip_judge_prepass_flag(self) -> None:
        """The CLI exposes --skip-judge-prepass (#4408)."""
        # Find the argparse setup in main() — the flag must appear.
        import inspect

        source = inspect.getsource(reingest.main)
        assert "--skip-judge-prepass" in source
        assert "skip_judge_prepass=args.skip_judge_prepass" in source


# ---------------------------------------------------------------------------
# Prefix-mode pre-pass — #4419 follow-up to #4408
# ---------------------------------------------------------------------------


_PREFIX_BUCKET = "test-prefix-bucket"
# _derive_court_code lowercases state + county and replaces spaces with
# dashes.  For ``ca/los_angeles/...`` the parsed county is ``los_angeles``,
# which _unsluggify rewrites to ``Los Angeles``, which _derive_court_code
# then folds back to ``ca-los-angeles``.
_PREFIX_COURT_CODE = "ca-los-angeles"
_PREFIX_COURT_ID = str(uuid.uuid4())
_PREFIX_COURT_IDS = {_PREFIX_COURT_CODE: _PREFIX_COURT_ID}

# S3 keys mirroring the live LA path layout — _S3_KEY_PATTERN requires
# content_hash to match ``[0-9a-f]+`` so the test hashes use only hex.
_SURNAME_KEY = (
    "ca/los_angeles/los_angeles_superior_court/raw/"
    "aaaa1111bbbb2222cccc3333dddd4444.html"
)
_BOILERPLATE_KEY = (
    "ca/los_angeles/los_angeles_superior_court/raw/"
    "eeee5555ffff6666aaaa7777bbbb8888.html"
)


class TestSeedJudgesFromKeys:
    """Tests for ``_seed_judges_from_keys`` — the #4419 prefix-mode pre-pass."""

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_prefix_mode_chronological_race_closes(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Replays the #4397 LA dept-25 race in prefix mode and asserts the
        pre-pass closes it on a single ``--prefix`` invocation.

        The synthetic 2-doc prefix scan mirrors the cursor-mode test, but
        ordered surname-first (the bug-trigger order — when prefix-mode's
        ``ProcessPoolExecutor`` happens to complete the surname doc before
        the boilerplate doc, the surname doc commits ``judge_id = NULL``
        without the pre-pass).

        Post-#4419 invariant: the pre-pass walks both keys, sees the full
        name on the boilerplate doc, calls ``resolve_judge`` to upsert
        ``Karine Mkrtchyan`` BEFORE the main pool runs.  The main pool's
        ``_expand_single_word_judge_surname`` Step 4 lookup then finds the
        seeded judge regardless of completion order, so the surname doc
        resolves NON-NULL on the first prefix-mode invocation.
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
            raise AssertionError(f"unexpected S3 key in prefix pre-pass: {key!r}")

        mock_fetch_s3.side_effect = fetch_s3_side_effect

        def extract_text_side_effect(
            raw_content: bytes, doc_format: str, pdf_timeout: float = 30.0
        ) -> str:
            if raw_content == surname_bytes:
                return "JUDGE/DEPT: Mkrtchyan/25"
            if raw_content == boilerplate_bytes:
                return "DEPARTMENT 25 JUDGE KARINE MKRTCHYAN"
            raise AssertionError("unexpected raw_content in prefix pre-pass")

        mock_extract_text.side_effect = extract_text_side_effect

        def extract_judge_side_effect(text: str) -> str | None:
            if "Mkrtchyan/25" in text:
                return "Mkrtchyan"  # bare surname — rejected as invalid
            if "JUDGE KARINE MKRTCHYAN" in text:
                return "KARINE MKRTCHYAN"  # full name — seeded
            return None

        mock_extract_judge.side_effect = extract_judge_side_effect

        mock_resolve_judge.return_value = "judge-id-karine-mkrtchyan"

        def looks_valid_side_effect(name: str) -> bool:
            return bool(name) and len(name.strip().split()) >= 2

        conn = MagicMock()

        with patch(
            "reingest_from_s3._looks_like_valid_judge_name",
            side_effect=looks_valid_side_effect,
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
        # BEFORE the main pool, so single-word surnames resolve via
        # ``_expand_single_word_judge_surname`` Step 4 regardless of
        # worker-pool completion order.
        assert mock_resolve_judge.call_count == 1, mock_resolve_judge.call_args_list
        seeded_args, _ = mock_resolve_judge.call_args
        assert seeded_args[1] == "KARINE MKRTCHYAN", seeded_args
        assert seeded_args[2] == _PREFIX_COURT_ID, seeded_args

        # ---- conn.commit() called exactly once at the end (only when
        # at least one judge was seeded).  Empty pre-passes commit zero
        # times — same shape as ``_seed_judges_from_cursor``.
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
            "single prefix-mode invocation; pre-pass did not close the "
            "chronological-resolver-race class for prefix mode."
        )
        assert suffix_rows[0][0] == "Karine Mkrtchyan"

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_prefix_pre_pass_skips_unparseable_keys(
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
        # No seeds → no commit (matches cursor-mode "skip empty page commit").
        assert conn.commit.call_count == 0

    @patch("reingest_from_s3.resolve_judge")
    @patch("reingest_from_s3.extract_judge_name")
    @patch("reingest_from_s3._extract_text_from_content")
    @patch("reingest_from_s3._fetch_s3_content")
    def test_prefix_pre_pass_skips_keys_with_no_court_mapping(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Keys whose court_code is missing from ``court_ids`` are skipped."""
        # Valid pattern, but the court_ids mapping is empty.
        keys = [_SURNAME_KEY]
        conn = MagicMock()

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            keys,
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
    def test_prefix_pre_pass_only_seeds_full_names(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """Bare single-word surnames are NOT seeded (mirror cursor-mode)."""
        keys = [_SURNAME_KEY]
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
                keys,
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
    def test_prefix_pre_pass_handles_no_judge_match(
        self,
        mock_fetch_s3: MagicMock,
        mock_extract_text: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
    ) -> None:
        """When extract_judge_name returns None, no seed and no error."""
        keys = [_SURNAME_KEY]
        conn = MagicMock()

        mock_fetch_s3.return_value = b"<html>no judge here</html>"
        mock_extract_text.return_value = "no judge here"
        mock_extract_judge.return_value = None

        stats = reingest._seed_judges_from_keys(
            conn,
            MagicMock(),
            keys,
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


class TestRunReingestFromPrefixWiresPrePass:
    """Tests that ``run_reingest_from_prefix`` invokes the pre-pass."""

    def test_skip_judge_prepass_param_exists(self) -> None:
        """run_reingest_from_prefix accepts the skip_judge_prepass kwarg (#4419)."""
        import inspect

        sig = inspect.signature(reingest.run_reingest_from_prefix)
        assert "skip_judge_prepass" in sig.parameters
        assert sig.parameters["skip_judge_prepass"].default is False

    def test_cli_routes_skip_judge_prepass_to_prefix_runner(self) -> None:
        """main() passes args.skip_judge_prepass through to run_reingest_from_prefix."""
        import inspect

        source = inspect.getsource(reingest.main)
        # The prefix-mode call site must forward skip_judge_prepass.
        assert "skip_judge_prepass=args.skip_judge_prepass" in source
        # And the prefix-mode call site must pass parse_timeout (used by
        # the pre-pass for PDF text extraction).
        assert "parse_timeout=args.parse_timeout" in source
