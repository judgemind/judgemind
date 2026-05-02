"""CI guard: every v2-daemon query against ``dispatcher.terminal_outcomes``
must be scoped to v2-owned rows during v2/v3 cohabitation (#3918).

Why this test exists
--------------------
v3 cohabs the same ``dispatcher.terminal_outcomes`` table as v2 during the
rollout window. Per-row ownership is established via ``parent_run_id`` →
``runs.dispatcher_version`` (column added by migration 56, #3872). v2's
circuit-breaker code paths run bulk SELECTs against
``dispatcher.terminal_outcomes``; without a v2-scoping filter, those paths
would silently count v3-owned failure rows toward v2's breaker threshold and
prematurely flip v2's ``concurrency_cap``. The INSERT site must also write
``parent_run_id`` so that the read-side filter has a value to match on.

The three sites identified in ``daemon.py`` (issue #3918):

* ``_write_terminal_outcome`` — INSERT with ``parent_run_id`` in the column
  list (writes v2's run-id so read-side filters work correctly).
* ``_evaluate_circuit_breaker`` — SELECT with
  ``V2_SCOPED_PARENT_RUN_FILTER`` (cohabitation filter).
* ``_cb_current_bad_count`` — SELECT with ``V2_SCOPED_PARENT_RUN_FILTER``
  (cohabitation filter).

Each query against ``dispatcher.terminal_outcomes`` added to ``daemon.py``
(or to ``daily_report.py``) must satisfy at least one of:

1. The query SQL contains ``parent_run_id`` (either inlined, or the
   ``{V2_SCOPED_PARENT_RUN_FILTER}`` constant substitution as it appears
   literally in the source file before Python evaluates the f-string).
2. An adjacent ``# v2-scoped: <reason>`` marker comment whitelists the
   query (use this for the rare exception). The reason follows a short
   controlled-vocabulary set:

   * ``parent-run-id-filter`` — SQL contains the cohabitation filter (case 1
     above; the comment is informational, the test would pass on the SQL
     alone).
   * ``daemon-write-with-run-id`` — INSERT writes
     ``parent_run_id = self._run_id`` from a daemon whose run row carries
     ``dispatcher_version='v2'``. This is the source of truth for case 1
     reads.

Note: ``by-agent-id`` does NOT apply to ``dispatcher.terminal_outcomes``.
Terminal outcomes are not keyed by a UUID PK that provides intrinsic
v2/v3 scoping — there is no UUID-PK escape hatch for this table.

How the test works
------------------
The test reads ``daemon.py`` and ``daily_report.py`` as plain text, walks
every line that contains ``FROM dispatcher.terminal_outcomes``,
``UPDATE dispatcher.terminal_outcomes``, or
``INSERT INTO dispatcher.terminal_outcomes``, expands the surrounding
``cur.execute(...)`` block, and applies the two-clause recognizer above.

Failures point at the exact line number and report which clause was
expected. The pre-PR contract is: add ``parent_run_id`` to the SQL (or
the ``{V2_SCOPED_PARENT_RUN_FILTER}`` constant reference), or add the
marker comment — the test is happy with either.

Adding a deliberate regression
------------------------------
:func:`test_unscoped_read_detected_in_synthetic_fixture` and
:func:`test_unscoped_insert_detected_in_synthetic_fixture` prove the test
catches unscoped queries: each constructs a synthetic source string in
memory (no edits to daemon.py) and asserts the classifier flags it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Files audited
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parents[2]
_DAEMON_PY = _SCRIPTS / "dispatcher" / "daemon.py"
_DAILY_REPORT_PY = _SCRIPTS / "dispatcher" / "daily_report.py"

# ---------------------------------------------------------------------------
# Recognizer regexes
# ---------------------------------------------------------------------------

# Match SQL statements touching ``dispatcher.terminal_outcomes`` — not
# docstrings or comments. We anchor on the verb (FROM / UPDATE /
# INSERT INTO) followed by ``dispatcher.terminal_outcomes`` since that's
# the part of the SQL that signals a real query.
_VERB_PATTERN = re.compile(
    r"(?:FROM|UPDATE|INSERT\s+INTO)\s+dispatcher\.terminal_outcomes\b",
    re.IGNORECASE,
)

# Lines that are pure comments or docstrings — those don't represent real
# SQL even when they happen to mention ``FROM dispatcher.terminal_outcomes``
# (e.g. a docstring explaining what a query does). A line whose stripped
# form starts with ``#`` is a comment; one starting with ``"""`` or ``'''``
# opens/closes a docstring. We also skip lines with no quote characters.
_COMMENT_LINE = re.compile(r"^\s*#")
_DOCSTRING_BOUNDARY = re.compile(r'^\s*("""|\'\'\')')

# The cohabitation filter in either inlined form (``parent_run_id``) or the
# constant-substitution form as it appears literally in source before
# Python evaluates the f-string (``{V2_SCOPED_PARENT_RUN_FILTER}``).
#
# This two-alternative pattern is required because the SELECT sites in
# daemon.py use f-strings:
#   f"  AND t.{V2_SCOPED_PARENT_RUN_FILTER} "
# When the file is read as plain text, the literal characters are
# ``{V2_SCOPED_PARENT_RUN_FILTER}`` — ``\bparent_run_id\b`` alone would
# not match. Matching the constant name covers both cases cleanly.
_PARENT_RUN_FILTER = re.compile(
    r"\bparent_run_id\b|\bV2_SCOPED_PARENT_RUN_FILTER\b",
)

# Marker comment — a previously-considered exception with an explanatory
# tag. Note: ``by-agent-id`` is intentionally absent; terminal_outcomes
# has no UUID-PK escape hatch analogous to ``dispatcher.agents``.
_SCOPING_MARKER = re.compile(
    r"#\s*v2-scoped:\s*(parent-run-id-filter|daemon-write-with-run-id)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------


def _extract_query_blocks(source: str) -> list[tuple[int, str, str]]:
    """Yield ``(line_no, block_text, preceding_context)`` for every SQL
    statement in *source* that touches ``dispatcher.terminal_outcomes``.

    The "block" is the smallest contiguous substring that spans both
    the first line of the SQL literal AND the closing ``)`` of the
    enclosing ``cur.execute(...)`` (or the closing triple-quote of a
    top-level multi-line string assignment for ``daily_report.py``).

    The "preceding context" is the up-to-12 source lines immediately
    above the block — used to find ``# v2-scoped:`` marker comments.

    Comment / docstring lines that happen to mention
    ``FROM dispatcher.terminal_outcomes`` (e.g. function docstrings
    explaining what a method does) are ignored.
    """
    lines = source.splitlines()
    n = len(lines)
    blocks: list[tuple[int, str, str]] = []
    seen_starts: set[int] = set()

    # Pre-pass: identify line ranges that are inside a top-level
    # ``_SQL_X = """ ... """`` assignment. Lines in those ranges are
    # bare SQL prose (no leading ``"``) but are real queries we must
    # audit. Outside those ranges, we require a leading ``"`` to
    # discriminate real SQL string literals from docstring prose that
    # happens to mention ``UPDATE dispatcher.terminal_outcomes``.
    sql_literal_ranges: list[tuple[int, int]] = []
    in_sql_literal = False
    sql_start = -1
    for idx, raw in enumerate(lines):
        if not in_sql_literal:
            if re.search(r'_SQL_[A-Z_]+\s*=\s*("""|\'\'\')', raw):
                in_sql_literal = True
                sql_start = idx
        else:
            if '"""' in raw or "'''" in raw:
                sql_literal_ranges.append((sql_start, idx))
                in_sql_literal = False

    def _in_sql_literal_block(idx: int) -> bool:
        for s, e in sql_literal_ranges:
            if s <= idx <= e:
                return True
        return False

    for i, line in enumerate(lines):
        if not _VERB_PATTERN.search(line):
            continue
        # Skip pure-comment lines.
        if _COMMENT_LINE.match(line):
            continue
        # Discriminate real SQL from docstring prose. For lines inside a
        # top-level ``_SQL_X = """ ... """`` assignment (daily_report
        # style), every match is real. Outside such a block, we require
        # the line's stripped form to start with ``"`` — a Python string
        # literal opener.
        stripped = line.lstrip()
        if not _in_sql_literal_block(i) and not stripped.startswith('"'):
            continue
        # Find the start of the enclosing ``cur.execute(`` call (or
        # top-level ``_SQL_X = """`` assignment) by walking backwards
        # up to 25 lines (some daemon.py SELECTs are 15+ lines of
        # column projections before the ``FROM`` clause).
        block_start = i
        for back in range(1, 26):
            if i - back < 0:
                break
            prev = lines[i - back]
            if "cur.execute(" in prev or re.search(
                r'_SQL_[A-Z_]+\s*=\s*("""|\'\'\')', prev
            ):
                block_start = i - back
                break
        if block_start in seen_starts:
            continue
        seen_starts.add(block_start)
        # Walk forward to find the end of the SQL block. Two end
        # conditions, whichever fires first:
        #
        # 1. ``cur.execute(...)`` style: a line whose stripped form is
        #    exactly ``)`` or ``),`` — the syntactic close of the Python
        #    call. We can't naively count parens because the SQL inside
        #    string literals has many ``(`` / ``)`` characters
        #    (``EXTRACT(...)``, ``COALESCE(...)``, etc.) which would
        #    skew the count.
        # 2. ``_SQL_X = """ ... """`` style: a closing triple-quote.
        block_end = block_start
        triple_quoted = '"""' in lines[block_start] or "'''" in lines[block_start]
        for j in range(block_start + 1, min(n, block_start + 50)):
            block_end = j
            stripped_j = lines[j].strip()
            if stripped_j in (")", "),"):
                break
            if triple_quoted and ('"""' in lines[j] or "'''" in lines[j]):
                break
        block_text = "\n".join(lines[block_start : block_end + 1])
        # Walk backward up to 12 lines for marker comments.
        ctx_start = max(0, block_start - 12)
        preceding = "\n".join(lines[ctx_start:block_start])
        blocks.append((block_start + 1, block_text, preceding))
    return blocks


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def _is_scoped(block_text: str, preceding: str) -> tuple[bool, str]:
    """Return ``(scoped, reason)`` for one query block.

    Order of precedence:
      1. SQL contains ``parent_run_id`` or ``V2_SCOPED_PARENT_RUN_FILTER``
         -> scoped via cohabitation filter.
      2. Adjacent ``# v2-scoped: <reason>`` marker -> whitelisted.
    """
    if _PARENT_RUN_FILTER.search(block_text):
        return True, "parent_run_id / V2_SCOPED_PARENT_RUN_FILTER present in SQL"
    marker = _SCOPING_MARKER.search(preceding)
    if marker:
        return True, f"v2-scoped marker: {marker.group(1)}"
    return False, (
        "no parent_run_id filter, no V2_SCOPED_PARENT_RUN_FILTER reference, "
        "and no '# v2-scoped: <reason>' marker comment"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _audit_file(path: Path) -> list[tuple[int, str, str]]:
    """Run the audit on a single source file. Returns offending blocks."""
    source = path.read_text()
    blocks = _extract_query_blocks(source)
    offenders: list[tuple[int, str, str]] = []
    for line_no, block_text, preceding in blocks:
        scoped, _reason = _is_scoped(block_text, preceding)
        if not scoped:
            offenders.append((line_no, block_text, preceding))
    return offenders


def test_daemon_py_terminal_outcomes_queries_are_scoped() -> None:
    """Every ``dispatcher.terminal_outcomes`` query in daemon.py is v2-scoped."""
    offenders = _audit_file(_DAEMON_PY)
    if offenders:
        msgs = []
        for line_no, block_text, _preceding in offenders:
            preview = block_text[:300].replace("\n", " | ")
            msgs.append(f"  line {line_no}: {preview}")
        pytest.fail(
            "Found unscoped ``dispatcher.terminal_outcomes`` queries in daemon.py.\n"
            "Each match must include `parent_run_id` or `V2_SCOPED_PARENT_RUN_FILTER` "
            "in the SQL, or a `# v2-scoped: <reason>` marker comment within ~12 lines "
            "above the `cur.execute(`. See "
            "`scripts/dispatcher/tests/test_terminal_outcomes_query_scoping.py` "
            "docstring + #3918 for the rationale.\n\n"
            "Offending sites:\n" + "\n".join(msgs)
        )


def test_daily_report_py_terminal_outcomes_queries_are_scoped() -> None:
    """Every ``dispatcher.terminal_outcomes`` query in daily_report.py is v2-scoped.

    Currently 0 sites — passes vacuously. Future-proofs against new queries
    being added to daily_report.py without the cohabitation filter.
    """
    offenders = _audit_file(_DAILY_REPORT_PY)
    if offenders:
        msgs = []
        for line_no, block_text, _preceding in offenders:
            preview = block_text[:300].replace("\n", " | ")
            msgs.append(f"  line {line_no}: {preview}")
        pytest.fail(
            "Found unscoped ``dispatcher.terminal_outcomes`` queries in "
            "daily_report.py. The daily report runs during cohabitation; "
            "without scoping it mixes v3 rows into v2's metrics. See #3918.\n\n"
            "Offending sites:\n" + "\n".join(msgs)
        )


def test_audit_finds_at_least_floor_blocks_in_daemon_py() -> None:
    """Sanity check: the audit walker finds the known sites in daemon.py.

    Without this, a regex regression that fails to find any
    ``dispatcher.terminal_outcomes`` lines at all would make
    :func:`test_daemon_py_terminal_outcomes_queries_are_scoped` pass
    vacuously. We assert the floor count is at least 3 (the 1 INSERT +
    2 SELECTs at L19452, L19552-63, L20163-74).
    """
    blocks = _extract_query_blocks(_DAEMON_PY.read_text())
    assert len(blocks) >= 3, (
        f"audit walker found only {len(blocks)} query blocks in "
        "daemon.py; expected ≥3 (1 INSERT + 2 SELECTs). "
        "Check the regex didn't regress."
    )


def test_unscoped_read_detected_in_synthetic_fixture() -> None:
    """Adversarial test: a deliberately-unscoped SELECT is flagged.

    Builds a synthetic source string containing one fully-unscoped
    SELECT and asserts the classifier flags it. This proves the test
    isn't trivially passing — if the regex regressed to "always
    return scoped=True", this test would fail. AC #1 from #3918
    (deliberately-introduced unscoped read is caught).
    """
    synthetic = '''
def _bad_read(self):
    """A deliberately unscoped sweep — should trip the audit."""
    with self._conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM dispatcher.terminal_outcomes WHERE status = 'failed'",
        )
        return cur.fetchall()
'''
    blocks = _extract_query_blocks(synthetic)
    assert len(blocks) == 1, (
        f"expected exactly 1 query block in synthetic fixture, got {len(blocks)}"
    )
    line_no, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert not scoped, (
        f"synthetic unscoped read was incorrectly classified as scoped: "
        f"reason={reason!r}, block={block_text!r}"
    )


def test_unscoped_insert_detected_in_synthetic_fixture() -> None:
    """Adversarial test: a deliberately-unscoped INSERT is flagged.

    Builds a synthetic source string containing an INSERT into
    ``dispatcher.terminal_outcomes`` without ``parent_run_id`` in the
    column list and asserts the classifier flags it.
    """
    synthetic = '''
def _bad_insert(self, agent_id, status):
    """An INSERT that omits parent_run_id — should trip the audit."""
    with self._conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dispatcher.terminal_outcomes "
            "    (agent_id, status, ended_at) "
            "VALUES (%s, %s, now())",
            (agent_id, status),
        )
'''
    blocks = _extract_query_blocks(synthetic)
    assert len(blocks) == 1, (
        f"expected exactly 1 query block in synthetic fixture, got {len(blocks)}"
    )
    line_no, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert not scoped, (
        f"synthetic unscoped INSERT was incorrectly classified as scoped: "
        f"reason={reason!r}, block={block_text!r}"
    )


def test_scoped_read_with_filter_passes() -> None:
    """Positive control: a SELECT with ``{V2_SCOPED_PARENT_RUN_FILTER}`` passes.

    The f-string constant reference appears as literal text in the source
    file before Python evaluates it — the recognizer must match it.
    """
    synthetic = """
def _good_read(self):
    with self._conn.cursor() as cur:
        cur.execute(
            "SELECT t.status "
            "FROM dispatcher.terminal_outcomes t "
            "WHERE t.ended_at > now() - make_interval(mins => %s) "
            f"  AND t.{V2_SCOPED_PARENT_RUN_FILTER} "
            "ORDER BY t.ended_at DESC "
            "LIMIT %s",
            (window_minutes, window_size),
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks, "expected at least 1 query block in positive-control fixture"
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, (
        f"scoped read with V2_SCOPED_PARENT_RUN_FILTER was incorrectly flagged: "
        f"reason={reason!r}"
    )


def test_scoped_insert_with_column_passes() -> None:
    """Positive control: an INSERT with ``parent_run_id`` in column list passes."""
    synthetic = """
def _good_insert(self, agent_id, issue_number, status, parent_run_id):
    with self._conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dispatcher.terminal_outcomes "
            "    (agent_id, issue_number, status, parent_run_id, ended_at) "
            "VALUES (%s, %s, %s, %s, now())",
            (agent_id, issue_number, status, parent_run_id),
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks, "expected at least 1 query block in positive-control fixture"
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, (
        f"scoped INSERT with parent_run_id column was incorrectly flagged: "
        f"reason={reason!r}"
    )


def test_scoped_query_by_marker_comment_passes() -> None:
    """Positive control: an explicit ``# v2-scoped:`` marker whitelists a block."""
    synthetic = """
def _marked(self):
    with self._conn.cursor() as cur:
        # v2-scoped: parent-run-id-filter (uses V2_SCOPED_PARENT_RUN_FILTER, see #3918)
        cur.execute(
            "SELECT status FROM dispatcher.terminal_outcomes WHERE status = 'failed'",
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks, "expected at least 1 query block in positive-control fixture"
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, f"marker-tagged query was incorrectly flagged: reason={reason!r}"
