"""CI guard: every v2-daemon query against ``dispatcher.agents`` must
be scoped to v2-owned rows during v2/v3 cohabitation (#3875).

Why this test exists
--------------------
v3 cohabits the same ``dispatcher.agents`` table as v2 during the
rollout window. Per-row ownership is established via
``parent_run_id`` → ``runs.dispatcher_version`` (column added by
migration 56, #3872). v2's recovery / sweep / reaper code paths run
bulk SELECTs and UPDATEs against ``dispatcher.agents``; without a
v2-scoping filter, those paths would silently corrupt v3-owned rows:

* :func:`_clear_stale_agent_task_arns` would null v3's task ARNs at
  the 2h mark — the most destructive site.
* :func:`recover_abandoned_agents` would flip v3 rows to
  ``status='crashed'`` + ``phase='daemon_restart_abandoned'`` on
  every v2 redeploy.
* :func:`_check_stuck_agents` would issue ``ecs:StopTask`` against
  v3's running Fargate tasks.
* :func:`_advance_running_agents` would drive v3 agents through
  v2's merge / verify / retro state machine.
* :func:`_reap_completed_agent_tasks` would route v3's STOPPED
  transitions through v2's ``_handle_agent_failure`` path.

The ≥8 such sites identified by the v3-spec adversarial review (issue
#3875) are scoped via the :data:`daemon.V2_SCOPED_PARENT_RUN_FILTER`
constant. This test enforces the invariant going forward — any new
query against ``dispatcher.agents`` added to ``daemon.py`` (or to
``daily_report.py``) must satisfy at least one of:

1. The query SQL contains ``parent_run_id`` (the cohabitation filter,
   typically supplied via ``V2_SCOPED_PARENT_RUN_FILTER``).
2. The query is a row-PK lookup (``WHERE agent_id = %s`` or
   ``WHERE a.agent_id = %s``). Such lookups are intrinsically scoped
   because ``agent_id`` is a UUID PK and v3's UUIDs are minted
   independently of v2 — v2 only has ``agent_id`` values for rows it
   itself inserted.
3. An adjacent ``# v2-scoped: <reason>`` marker comment whitelists
   the query (use this for the rare exception). The reason follows a
   short controlled-vocabulary set:

   * ``parent-run-id-filter`` — SQL contains the cohabitation filter
     (case 1 above; the comment is informational, the test would
     pass on the SQL alone).
   * ``by-agent-id`` — SQL is a UUID-PK lookup (case 2 above; the
     comment is informational, the test would pass on the SQL alone).
   * ``daemon-write-with-run-id`` — INSERT writes
     ``parent_run_id = self._run_id`` from a daemon whose run row
     carries ``dispatcher_version='v2'``. This is the source of
     truth for case 1 reads.

This test was added as the mandatory CI guard called out in issue
#3875's acceptance criteria. Without it, a future PR can introduce
an unscoped query and silently re-open the corruption window.

How the test works
------------------
The test reads ``daemon.py`` and ``daily_report.py`` as plain text,
walks every line that contains ``FROM dispatcher.agents``,
``UPDATE dispatcher.agents``, or ``INSERT INTO dispatcher.agents``,
expands the surrounding ``cur.execute(...)`` block (or top-level SQL
literal for ``daily_report.py``), and applies the three-clause
recognizer above.

Failures point at the exact line number and report which clause was
expected. The pre-PR contract is: add the filter, the row-PK
condition, or the marker comment — the test is happy with any of
the three.

Adding a deliberate regression
------------------------------
:func:`test_unscoped_query_detected_in_synthetic_fixture` proves the
test catches an unscoped query: it constructs a synthetic source
string in memory (no edits to daemon.py) containing
``SELECT * FROM dispatcher.agents WHERE 1=1`` and asserts the
classifier flags it.
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

# Match SQL statements touching ``dispatcher.agents`` — not docstrings or
# comments. We anchor on the verb (FROM / UPDATE / INSERT INTO) followed
# by ``dispatcher.agents`` since that's the part of the SQL that signals
# a real query (comments / docstrings reference the table by full name
# but rarely with these verb prefixes inside a string literal).
_VERB_PATTERN = re.compile(
    r"(?:FROM|UPDATE|INSERT\s+INTO)\s+dispatcher\.agents\b",
    re.IGNORECASE,
)

# Lines that are pure comments or docstrings — those don't represent
# real SQL even when they happen to mention ``FROM dispatcher.agents``
# (e.g. a docstring explaining what a query does). The simplest
# heuristic: a line whose stripped form starts with ``#`` is a comment,
# and a line whose stripped form starts with ``"""`` or ``'''`` opens or
# closes a docstring. We also skip any line that does NOT contain a
# Python string literal (no quote characters at all) — those are
# free-form prose only.
_COMMENT_LINE = re.compile(r"^\s*#")
_DOCSTRING_BOUNDARY = re.compile(r'^\s*("""|\'\'\')')

# A row-PK lookup is an intrinsically-safe read because ``agent_id`` is
# a UUID minted by ``gen_random_uuid()`` and v2 only has a v2-owned
# ``agent_id`` to query with — v3 never hands a UUID into a v2 code
# path. Recognize both bare and prefix-aliased forms.
_BY_AGENT_ID = re.compile(r"\bWHERE\b.*\b(?:[a-z]\.)?agent_id\s*=\s*%s")

# The cohabitation filter, in either inlined form or the constant
# substitution form.
_PARENT_RUN_FILTER = re.compile(r"\bparent_run_id\b")

# Marker comment — a previously-considered exception with an
# explanatory tag.
_SCOPING_MARKER = re.compile(
    r"#\s*v2-scoped:\s*(parent-run-id-filter|by-agent-id|daemon-write-with-run-id)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------


def _extract_query_blocks(source: str) -> list[tuple[int, str, str]]:
    """Yield ``(line_no, block_text, preceding_context)`` for every SQL
    statement in *source* that touches ``dispatcher.agents``.

    The "block" is the smallest contiguous substring that spans both
    the first line of the SQL literal AND the closing ``)`` of the
    enclosing ``cur.execute(...)`` (or the closing triple-quote of a
    top-level multi-line string assignment for ``daily_report.py``).

    The "preceding context" is the up-to-12 source lines immediately
    above the block — used to find ``# v2-scoped:`` marker comments.

    Comment / docstring lines that happen to mention
    ``FROM dispatcher.agents`` (e.g. function docstrings explaining
    what a method does) are ignored.
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
    # happens to mention ``UPDATE dispatcher.agents``.
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
        # Skip pure-comment lines (``#: like FROM dispatcher.agents`` etc).
        if _COMMENT_LINE.match(line):
            continue
        # Discriminate real SQL from docstring prose. For lines inside
        # a top-level ``_SQL_X = """ ... """`` assignment (daily_report
        # style), every match is real. Outside such a block, we
        # require the line's stripped form to start with ``"`` — a
        # Python string literal opener.
        stripped = line.lstrip()
        if not _in_sql_literal_block(i) and not stripped.startswith('"'):
            continue
        # Find the start of the enclosing ``cur.execute(`` call (or
        # top-level ``_SQL_X = """`` assignment) by walking backwards
        # up to 25 lines (some daemon.py SELECTs are 15+ lines of
        # column projections before the ``FROM`` clause — e.g.
        # ``_check_stuck_agents`` is 15 lines pre-FROM).
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
        # 1. ``cur.execute(...)`` style: a line whose stripped form
        #    is exactly ``)`` or ``),`` — the syntactic close of the
        #    Python call. We can't naively count parens because the
        #    SQL inside string literals has many ``(`` / ``)``
        #    characters (``EXTRACT(...)``, ``COALESCE(...)``,
        #    ``LEFT JOIN LATERAL(...)``) which would skew the count.
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
        # Walk backward up to 12 lines for marker comments. We stop at
        # the first non-comment, non-blank, non-string line (typically
        # ``with self._conn.cursor() as cur:``) — markers must be
        # adjacent or in the same logical block.
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
      1. SQL contains ``parent_run_id`` -> scoped via cohabitation filter.
      2. SQL has ``WHERE [alias.]agent_id = %s`` -> intrinsically scoped.
      3. Adjacent ``# v2-scoped: <reason>`` marker -> whitelisted.
    """
    if _PARENT_RUN_FILTER.search(block_text):
        return True, "parent_run_id present in SQL"
    if _BY_AGENT_ID.search(block_text):
        return True, "WHERE agent_id = %s (UUID PK lookup)"
    marker = _SCOPING_MARKER.search(preceding)
    if marker:
        return True, f"v2-scoped marker: {marker.group(1)}"
    return False, (
        "no parent_run_id filter, no WHERE agent_id = %s, "
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


def test_daemon_py_queries_are_v2_scoped() -> None:
    """Every ``dispatcher.agents`` query in daemon.py is v2-scoped."""
    offenders = _audit_file(_DAEMON_PY)
    if offenders:
        msgs = []
        for line_no, block_text, _preceding in offenders:
            preview = block_text[:300].replace("\n", " | ")
            msgs.append(f"  line {line_no}: {preview}")
        pytest.fail(
            "Found unscoped ``dispatcher.agents`` queries in daemon.py.\n"
            "Each match must include `parent_run_id` filtering, a "
            "`WHERE agent_id = %s` row-PK lookup, or a "
            "`# v2-scoped: <reason>` marker comment within ~12 lines "
            "above the `cur.execute(`. See "
            "`scripts/dispatcher/tests/test_agent_query_scoping.py` "
            "docstring + #3875 for the rationale.\n\n"
            "Offending sites:\n" + "\n".join(msgs)
        )


def test_daily_report_py_queries_are_v2_scoped() -> None:
    """Every ``dispatcher.agents`` query in daily_report.py is v2-scoped."""
    offenders = _audit_file(_DAILY_REPORT_PY)
    if offenders:
        msgs = []
        for line_no, block_text, _preceding in offenders:
            preview = block_text[:300].replace("\n", " | ")
            msgs.append(f"  line {line_no}: {preview}")
        pytest.fail(
            "Found unscoped ``dispatcher.agents`` queries in "
            "daily_report.py. The daily report is v2's operational "
            "summary; without scoping it mixes v3 rows into v2's "
            "metrics during cohabitation. See #3875.\n\n"
            "Offending sites:\n" + "\n".join(msgs)
        )


def test_audit_finds_at_least_one_block_per_file() -> None:
    """Sanity check: the audit walker is not silently returning empty.

    Without this, a regex regression that fails to find any
    ``dispatcher.agents`` lines at all would make
    :func:`test_daemon_py_queries_are_v2_scoped` pass vacuously. We
    assert the floor count is high enough to reflect reality without
    being so brittle that any edit breaks the test.
    """
    daemon_blocks = _extract_query_blocks(_DAEMON_PY.read_text())
    daily_blocks = _extract_query_blocks(_DAILY_REPORT_PY.read_text())
    # daemon.py has ~60 query sites; daily_report.py has 3.
    assert len(daemon_blocks) >= 30, (
        f"audit walker found only {len(daemon_blocks)} query blocks in "
        "daemon.py; expected ≥30. Check the regex didn't regress."
    )
    assert len(daily_blocks) >= 2, (
        f"audit walker found only {len(daily_blocks)} query blocks in "
        "daily_report.py; expected ≥2. Check the regex didn't regress."
    )


def test_unscoped_query_detected_in_synthetic_fixture() -> None:
    """Adversarial test: a deliberately-unscoped query is flagged.

    Builds a synthetic source string containing one fully-unscoped
    SELECT and asserts the classifier flags it. This proves the test
    isn't trivially passing — if the regex regressed to "always
    return scoped=True", this test would fail. AC #3 from #3875
    (deliberately-introduced unscoped query is caught).
    """
    synthetic = '''
def _bad_query(self):
    """A deliberately unscoped sweep — should trip the audit."""
    with self._conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id FROM dispatcher.agents WHERE status = 'running'",
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
        f"synthetic unscoped query was incorrectly classified as scoped: "
        f"reason={reason!r}, block={block_text!r}"
    )


def test_scoped_query_with_filter_passes() -> None:
    """Positive control: a query containing ``parent_run_id`` passes."""
    synthetic = """
def _good_query(self):
    with self._conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id FROM dispatcher.agents "
            "WHERE status = 'running' "
            "  AND parent_run_id IN ("
            "    SELECT run_id FROM dispatcher.runs WHERE dispatcher_version = 'v2'"
            ")",
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, f"scoped query was incorrectly flagged: reason={reason!r}"


def test_scoped_query_by_agent_id_passes() -> None:
    """Positive control: ``WHERE agent_id = %s`` is intrinsically safe."""
    synthetic = """
def _by_id(self, agent_id):
    with self._conn.cursor() as cur:
        cur.execute(
            "SELECT pr_number FROM dispatcher.agents WHERE agent_id = %s",
            (agent_id,),
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, f"by-agent-id query was incorrectly flagged: reason={reason!r}"


def test_scoped_query_by_marker_comment_passes() -> None:
    """Positive control: an explicit marker comment whitelists a block."""
    synthetic = """
def _marked(self):
    with self._conn.cursor() as cur:
        # v2-scoped: by-agent-id (UUID PK lookup, see #3875)
        cur.execute(
            "SELECT pr_number FROM dispatcher.agents WHERE agent_id = %s",
            (some_id,),
        )
"""
    blocks = _extract_query_blocks(synthetic)
    assert blocks
    _, block_text, preceding = blocks[0]
    scoped, reason = _is_scoped(block_text, preceding)
    assert scoped, f"marker-tagged query was incorrectly flagged: reason={reason!r}"
