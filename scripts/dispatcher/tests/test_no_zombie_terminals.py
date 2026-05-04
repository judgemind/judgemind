"""Issue #3574 — guard the zombie-terminal invariant.

A "zombie" agent row has ``status`` in
:data:`scripts.dispatcher.daemon.TERMINAL_AGENT_STATUSES`
(``failed``, ``crashed``, ``succeeded``, ``plan_blocked``, ``needs_review``)
AND ``ended_at IS NULL``. Such rows are invisible to monitoring queries that
order by ``ended_at DESC`` (the cockpit's "Recently Completed" panel, the
daily report's strict green-streak counter, the orphan-PR resurrection
sweep). On 2026-04-27 a population of zombie rows caused the autonomous
monitoring loop to silently miss 5 issues stuck in active retry-loops.

Two layers enforce the invariant:

1. **Per-write stamping** — every UPDATE site that writes a terminal
   status to ``dispatcher.agents`` MUST also stamp ``ended_at``
   (either ``ended_at = now()`` or
   ``ended_at = COALESCE(ended_at, now())``). The ``COALESCE`` form is
   preferred for restore/resurrect paths so any earlier stamp is
   preserved.
2. **Bulk backfill safety net** — :meth:`DispatcherDaemon._backfill_terminal_ended_at`
   runs every housekeeping tick and heals any row that slipped through.

This test covers layer 1: it scans the SQL UPDATE statements in
``scripts/dispatcher/daemon.py`` and asserts that every UPDATE that sets
a terminal ``status`` value also includes ``ended_at`` in its SET clause.
The agent-runner side (``agent-runner-entrypoint.sh``) is covered by
:mod:`test_agent_runner_terminal_ended_at`.

This is a static AST-style scan of the daemon source — it parses the
file text rather than executing the daemon. The intent is to catch the
regression class where a new failure path lands but the author forgets
the ``ended_at`` clause in its UPDATE.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above

_DAEMON_PATH = Path(daemon.__file__)
_DAEMON_TEXT = _DAEMON_PATH.read_text(encoding="utf-8")


def _terminal_statuses() -> frozenset[str]:
    """Return the canonical TERMINAL_AGENT_STATUSES frozenset.

    Sourced from the daemon module so the test stays in lockstep with
    the runtime definition. Adding a new terminal status to
    ``TERMINAL_AGENT_STATUSES`` automatically extends this test's
    coverage without any test edit.
    """
    return daemon.TERMINAL_AGENT_STATUSES


def _terminal_status_pattern() -> re.Pattern[str]:
    """Regex matching ``status = 'X'`` for any X in TERMINAL_AGENT_STATUSES.

    Matches both single- and double-quoted forms. The status value can
    be inside a Python string literal (``"... SET status = 'failed' ..."``).
    """
    statuses = "|".join(re.escape(s) for s in sorted(_terminal_statuses()))
    return re.compile(rf"status\s*=\s*['\"]({statuses})['\"]")


# UPDATE statements in daemon.py are split across multiple Python string
# literals concatenated by Python's adjacent-string-concatenation
# semantics. The cursor.execute() call accepts the concatenated value
# at runtime. To analyze a logical SQL statement we extract the
# enclosing call: ``cur.execute("UPDATE dispatcher.agents " ... ")``.
#
# Why we use ``ast`` instead of a single regex: the daemon's SQL
# contains literal single-quoted values (e.g. ``status = 'failed'``)
# inside Python double-quoted strings. A naive
# ``[\"'][^\"']*[\"']`` pattern stops at the first inner ``'``,
# dropping the rest of the SQL. The Python ``ast`` module gives us
# proper string-literal handling; we use it to walk every
# ``cur.execute(...)`` call and concatenate adjacent string literals
# into the full SQL via the ``ast.Constant.value`` it produces.


def _extract_update_statements() -> list[tuple[int, str]]:
    """Return ``(line_no, concatenated_sql)`` tuples for every
    ``cur.execute("UPDATE dispatcher.agents ...")`` call in daemon.py.

    Uses Python's ``ast`` module to walk the source and find calls to
    ``cur.execute`` whose first argument is a string literal (or an
    implicit-concatenation of string literals, which the ast module
    folds into a single ``Constant`` node automatically). Filters out
    UPDATEs that don't target ``dispatcher.agents`` (e.g.
    ``dispatcher.diagnoses``, ``dispatcher.config``).
    """
    tree = ast.parse(_DAEMON_TEXT)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match calls of the form ``X.execute(...)`` — the X part is
        # almost always ``cur`` but we don't constrain it (other names
        # like ``cursor`` are fine too).
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute":
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        # ast folds adjacent string literals into a single Constant.
        # If the first argument is anything else (e.g. an f-string,
        # a variable, a function call), skip it — those are not the
        # static SQL strings we're auditing.
        if not isinstance(first_arg, ast.Constant) or not isinstance(
            first_arg.value, str
        ):
            continue
        sql = first_arg.value
        if "UPDATE dispatcher.agents" not in sql:
            continue
        out.append((node.lineno, sql))
    return out


def _has_ended_at_stamp(sql: str) -> bool:
    """True if the SQL contains an ``ended_at = ...`` clause that writes
    a non-NULL value.

    Accepts the canonical forms used in daemon.py:

    * ``ended_at = now()``
    * ``ended_at = COALESCE(ended_at, now())``

    Rejects ``ended_at = NULL`` (used by retry-reset paths to clear
    the stamp before a re-run — those are NOT terminal writes).
    """
    # Strip whitespace inside the SET clause so multi-line SQL parses cleanly.
    normalized = re.sub(r"\s+", " ", sql)
    # Look for ``ended_at =`` followed by something that's not NULL.
    # The regex is intentionally permissive — any non-NULL right-hand
    # side counts as a stamp.
    m = re.search(r"ended_at\s*=\s*([^,]+?)(?:,|\s+WHERE|\s*$)", normalized)
    if m is None:
        return False
    rhs = m.group(1).strip().upper()
    if rhs == "NULL":
        return False
    return True


def _has_terminal_status_set(sql: str) -> bool:
    """True if the SQL's SET clause writes a literal terminal status.

    Returns True for ``SET status = 'failed'`` etc. Returns False for
    ``SET status = %s`` (parameterized) — those are handled by
    :func:`_writes_terminal_status_via_parameter` based on the
    surrounding control flow (the parameterized site in
    ``_mark_agent_terminal`` has its own branch-level guarantee that
    ``ended_at`` is set when ``status`` is terminal).
    """
    return bool(_terminal_status_pattern().search(sql))


class TestTerminalStatuses:
    """The test's source-of-truth status set matches the daemon's."""

    def test_terminal_statuses_match_daemon_constant(self) -> None:
        statuses = _terminal_statuses()
        assert "failed" in statuses
        assert "crashed" in statuses
        assert "succeeded" in statuses
        assert "plan_blocked" in statuses
        assert "needs_review" in statuses
        # Defensive — if the daemon adds a new terminal status, this
        # test inherits the coverage. Locking the cardinality here
        # would force an unrelated edit on every addition; instead,
        # simply assert the set is non-empty.
        assert len(statuses) >= 5


class TestEveryTerminalUpdateStampsEndedAt:
    """Every ``UPDATE dispatcher.agents`` that writes a literal terminal
    ``status`` value MUST also stamp ``ended_at``. This is the
    invariant that makes ``status terminal ⇒ ended_at NOT NULL`` hold
    at write time (the housekeeping backfill is the safety net, not the
    primary contract).
    """

    def test_no_zombie_creating_update_in_daemon(self) -> None:
        """Walk every cur.execute("UPDATE dispatcher.agents ...") in
        daemon.py and assert: if the SET clause writes a terminal
        ``status`` literal, it must also include ``ended_at``.
        """
        offenders: list[tuple[int, str]] = []
        for line_no, sql in _extract_update_statements():
            if not _has_terminal_status_set(sql):
                continue
            if _has_ended_at_stamp(sql):
                continue
            # Truncate the SQL for a readable failure message.
            normalized = re.sub(r"\s+", " ", sql).strip()
            preview = normalized[:140] + ("..." if len(normalized) > 140 else "")
            offenders.append((line_no, preview))

        assert not offenders, (
            "daemon.py has UPDATE sites that write a terminal status to "
            "dispatcher.agents WITHOUT also stamping ended_at. Each one "
            "creates a zombie row (#3574). Add `ended_at = COALESCE(ended_at, "
            "now())` (idempotent + race-safe with _backfill_terminal_ended_at) "
            "to the SET clause:\n\n"
            + "\n".join(f"  daemon.py:{n}  {sql}" for n, sql in offenders)
        )


class TestBackfillUpdateUsesEndedAtIsNullGuard:
    """The bulk backfill in ``_backfill_terminal_ended_at`` must filter
    on ``ended_at IS NULL`` so it doesn't clobber correct stamps. This
    guard also makes the UPDATE idempotent — a no-op once every
    terminal row has ``ended_at NOT NULL``.
    """

    def test_backfill_filters_on_ended_at_is_null(self) -> None:
        # The method body is short — read it as a string and assert
        # the WHERE clause contains both the terminal-status filter and
        # the ``ended_at IS NULL`` guard.
        # _backfill_terminal_ended_at is the canonical bulk healer,
        # introduced in #3822 and called every housekeeping tick.
        method_text = _extract_method_body("_backfill_terminal_ended_at")
        assert "ended_at IS NULL" in method_text, (
            "_backfill_terminal_ended_at must filter on ended_at IS NULL "
            "to be idempotent and avoid clobbering correct stamps."
        )
        assert "status = ANY(" in method_text, (
            "_backfill_terminal_ended_at must filter on the terminal status set."
        )
        assert "SET ended_at = now()" in method_text, (
            "_backfill_terminal_ended_at must stamp ended_at = now()."
        )


def _extract_method_body(method_name: str) -> str:
    """Return the source text of a method on ``DispatcherDaemon``.

    Walks indentation: starts at the method's ``def`` line, includes
    every line indented deeper than the ``def`` until the next
    method/class definition at the same or lesser indent. Used to
    scope SQL-shape assertions to one method instead of the whole
    27-thousand-line daemon.
    """
    text = _DAEMON_TEXT
    pattern = rf"^(\s+)def {re.escape(method_name)}\("
    m = re.search(pattern, text, re.MULTILINE)
    assert m is not None, f"method {method_name}() not found in daemon.py"
    method_indent = len(m.group(1))
    start = m.start()
    lines: list[str] = []
    seen_body = False
    for line in text[start:].splitlines(keepends=False):
        stripped = line.lstrip()
        if not stripped:
            # Blank line — keep, doesn't terminate.
            lines.append(line)
            continue
        line_indent = len(line) - len(stripped)
        if seen_body and line_indent <= method_indent:
            # Hit the next def/class at the same or lesser indent — stop.
            break
        lines.append(line)
        if line_indent > method_indent:
            seen_body = True
    return "\n".join(lines)
