# venv: none
"""Tests for ``scripts/_check_shipped_pr_verify_probe.py``.

The verify-probe helper (issue #4472) is the content-channel companion
to check-shipped-pr.sh's path-overlap heuristic. It extracts literal
``Verify:`` clauses from an issue body, classifies them by shape (grep
/ pytest / script-execution), and runs a safe subset against the
worktree to resolve the introducing PR.

These tests cover three layers:

  1. **Clause extraction** — the ``_extract_verify_clauses()`` parser.
     Verify lines come in many shapes — bare bullet, bold-emphasis
     (``**Verify:**``), backtick-wrapped command, indented continuation.
     The parser must extract the load-bearing command in each case
     without confusing trailing prose for command tokens.

  2. **Clause classification** — the ``_classify_clause()`` shape
     dispatcher. Decides whether a clause is grep / pytest / script /
     unsupported. Unsupported clauses are silently dropped (the helper
     must NOT execute arbitrary verbs like aws / curl / sql).

  3. **Probe execution + PR resolution** — end-to-end via a fixture
     git repository. Each test builds a temp git repo with deterministic
     content + commit history (subject lines carrying ``(#<n>)`` so the
     PR resolver can find them), runs the probe against it, and asserts
     the resolved PR number.

Loads the helper module via ``importlib.util.spec_from_file_location``
because the filename starts with an underscore — the standard
``import scripts._check_...`` path doesn't apply (``scripts/`` is not
a package).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_verify_probe.py"


def _import_probe_module():
    """Load the verify-probe helper as ``check_shipped_pr_verify_probe``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_verify_probe", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_verify_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe_module():
    return _import_probe_module()


# ─── Layer 1: Verify clause extraction ────────────────────────────────────


def test_extract_bullet_form(probe_module):
    """Plain bullet ``- Verify: <command>`` is extracted with bullet stripped."""
    body = (
        "## Acceptance criteria\n"
        "\n"
        "- [ ] Foo bar baz.\n"
        "  Verify: grep widget scripts/foo.sh\n"
    )
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["grep widget scripts/foo.sh"]


def test_extract_bold_form(probe_module):
    """``**Verify:**`` (bold-emphasis) is consumed by the prefix regex."""
    body = "  - **Verify:** pytest -k test_foo\n"
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["pytest -k test_foo"]


def test_extract_backtick_wrapped(probe_module):
    """Backtick-wrapped command is unwrapped — trailing prose is dropped."""
    body = (
        '  - **Verify:** `grep "WIDGET" scripts/foo/` returns a match; '
        "adding `--cwd` makes it fail.\n"
    )
    clauses = probe_module._extract_verify_clauses(body)
    # The backtick unwrap is non-greedy — captures the FIRST balanced
    # span. Trailing ``returns a match; adding --cwd makes it fail`` is
    # informational narrative, not part of the command.
    assert clauses == ['grep "WIDGET" scripts/foo/']


def test_extract_multiple_clauses(probe_module):
    """Multiple Verify lines in the same body each yield a clause."""
    body = (
        "- [ ] A\n"
        "  Verify: grep alpha scripts/a.sh\n"
        "- [ ] B\n"
        "  **Verify:** `pytest -k test_b`\n"
    )
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["grep alpha scripts/a.sh", "pytest -k test_b"]


def test_extract_ignores_lines_without_verify(probe_module):
    """Body lines that are not Verify clauses are skipped."""
    body = (
        "## Description\n"
        "Some prose about the feature.\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        "- [ ] Foo.\n"
        "  Verify: grep x y\n"
        "- [ ] Bar (manual).\n"
    )
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["grep x y"]


def test_extract_handles_case_insensitive(probe_module):
    """``verify:`` (lowercase) is extracted same as ``Verify:``."""
    body = "  verify: grep foo bar/\n"
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["grep foo bar/"]


def test_extract_strips_trailing_bold_closer(probe_module):
    """``Verify: <cmd>**`` (rare malformed bold) drops the closer."""
    body = "  Verify: grep foo bar/**\n"
    clauses = probe_module._extract_verify_clauses(body)
    assert clauses == ["grep foo bar/"]


# ─── Layer 2: Clause classification ───────────────────────────────────────


def test_classify_grep_clause(probe_module):
    """``grep ...`` is classified as a grep clause (issue #4472 AC3)."""
    cls = probe_module._classify_clause(
        "grep ALLOWED_CLAUDE_FLAGS scripts/dispatcher/tests/"
    )
    assert cls is not None
    shape, argv = cls
    assert shape == "grep"
    assert argv[0] == "grep"
    assert argv[1] == "ALLOWED_CLAUDE_FLAGS"


def test_classify_grep_clause_with_flags(probe_module):
    """``grep -n -r <pattern> <path>`` keeps the flags in argv."""
    cls = probe_module._classify_clause('grep -n -r "WIDGET" scripts/foo/')
    assert cls is not None
    shape, argv = cls
    assert shape == "grep"
    # The flags are preserved in argv; the pattern extractor walks past
    # them. Quoted "WIDGET" arrives as an unquoted token after shlex.
    assert argv[0] == "grep"
    assert "-n" in argv
    assert "-r" in argv


def test_classify_pytest_clause(probe_module):
    """``pytest -k <test_name>`` is classified as a pytest clause (AC4)."""
    cls = probe_module._classify_clause("pytest -k test_phase_constants_cover_all")
    assert cls is not None
    shape, argv = cls
    assert shape == "pytest"
    assert "-k" in argv


def test_classify_pytest_via_python_module(probe_module):
    """``python -m pytest -k <test>`` is also classified as pytest."""
    cls = probe_module._classify_clause("python -m pytest -k test_x")
    assert cls is not None
    shape, _ = cls
    assert shape == "pytest"


def test_classify_pytest3_via_python3_module(probe_module):
    """``python3 -m pytest`` (concrete interpreter) is also pytest."""
    cls = probe_module._classify_clause("python3 -m pytest -k test_x")
    assert cls is not None
    shape, _ = cls
    assert shape == "pytest"


def test_classify_script_dotslash_clause_drops(probe_module):
    """``./scripts/<probe>.sh`` is INTENTIONALLY unsupported (#4472).

    The verify-channel does not support script-execution clauses — see
    the "DELIBERATELY UNSUPPORTED" header in the helper module. Two
    reasons: arbitrary script execution is unsafe, and a pure-existence
    check is too weak a signal (issues that ask for an existing script
    to be EXTENDED would all false-positive). The path-overlap channel
    catches the cases this would have caught.
    """
    cls = probe_module._classify_clause("./scripts/check-foo.sh --flag value")
    assert cls is None


def test_classify_script_bash_clause_drops(probe_module):
    """``bash scripts/<probe>.sh`` is INTENTIONALLY unsupported (#4472)."""
    cls = probe_module._classify_clause("bash scripts/check-foo.sh")
    assert cls is None


def test_classify_unsupported_verbs_drop(probe_module):
    """Verbs we don't execute (aws, curl, gh, sql) classify as None.

    Safety contract — the helper executes ONLY grep and pytest --collect-only.
    Any other verb must fall through and the probe must return None,
    causing the wrapper to fall back to the path-overlap channel rather
    than executing arbitrary commands.
    """
    assert probe_module._classify_clause("aws ecs describe-clusters") is None
    assert probe_module._classify_clause("curl https://example.org/api") is None
    assert probe_module._classify_clause("gh issue view 42") is None
    assert (
        probe_module._classify_clause(
            "SELECT COUNT(*) FROM derived.rulings WHERE judge_id IS NULL"
        )
        is None
    )
    assert probe_module._classify_clause("reviewer confirms on read-through") is None


def test_classify_empty_clause_drops(probe_module):
    """Empty / whitespace-only clauses return None."""
    assert probe_module._classify_clause("") is None


def test_classify_unbalanced_quotes_drop(probe_module):
    """Unparseable clauses (unbalanced quotes) return None gracefully."""
    # shlex.split raises on unbalanced quotes; the classifier must
    # catch this and return None rather than crashing.
    result = probe_module._classify_clause('grep "unclosed quote scripts/foo')
    assert result is None


# ─── Layer 2b: argv accessors ─────────────────────────────────────────────


def test_extract_grep_pattern_basic(probe_module):
    """First non-flag token is the pattern."""
    assert (
        probe_module._extract_grep_pattern(["grep", "ALLOWED_CLAUDE_FLAGS", "scripts/"])
        == "ALLOWED_CLAUDE_FLAGS"
    )


def test_extract_grep_pattern_skips_flags(probe_module):
    """Flags before the pattern are skipped."""
    assert (
        probe_module._extract_grep_pattern(["grep", "-n", "-r", "WIDGET", "src/"])
        == "WIDGET"
    )


def test_extract_grep_pattern_returns_none_when_only_flags(probe_module):
    """argv with only flags returns None (no pattern recoverable)."""
    assert probe_module._extract_grep_pattern(["grep", "-n", "-r"]) is None


def test_extract_pytest_k_separate_token(probe_module):
    """``-k expr`` (separate tokens) is extracted."""
    assert (
        probe_module._extract_pytest_k_expr(["pytest", "-k", "test_foo"]) == "test_foo"
    )


def test_extract_pytest_k_joined_token(probe_module):
    """``-k=expr`` (joined) is extracted."""
    assert probe_module._extract_pytest_k_expr(["pytest", "-k=test_foo"]) == "test_foo"


def test_extract_pytest_keyword_long_form(probe_module):
    """``--keyword expr`` (long form) is extracted."""
    assert (
        probe_module._extract_pytest_k_expr(["pytest", "--keyword", "test_foo"])
        == "test_foo"
    )


def test_extract_pytest_keyword_long_form_joined(probe_module):
    """``--keyword=expr`` (long form joined) is extracted."""
    assert (
        probe_module._extract_pytest_k_expr(["pytest", "--keyword=test_foo"])
        == "test_foo"
    )


def test_extract_pytest_k_missing_returns_none(probe_module):
    """argv without -k returns None — the clause is unsupported."""
    assert probe_module._extract_pytest_k_expr(["pytest", "tests/"]) is None


# ─── Layer 3: End-to-end probes against a fixture git repo ────────────────


def _git_init_repo(tmp_path: Path) -> Path:
    """Initialize a tmp git repo with deterministic config.

    Sets a fixed user name + email so commits don't depend on the agent's
    global git config. Returns the repo root.
    """
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", "."],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Author"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo,
        check=True,
    )
    return repo


def _git_commit(
    repo: Path,
    *,
    message: str,
    files: dict[str, str],
    date: str | None = None,
) -> None:
    """Write ``files`` and commit them with ``message``.

    ``date`` (ISO-8601) pins both author and committer date so tests can
    place a commit before or after an issue's ``createdAt`` (#4666).
    """
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "add", rel_path], cwd=repo, check=True)
    env = None
    if date is not None:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
    subprocess.run(
        ["git", "commit", "--quiet", "-m", message], cwd=repo, check=True, env=env
    )


# An issue createdAt comfortably before any "now"-dated fixture commit, so
# the date-ordering guard (#4666) accepts fixture commits made without an
# explicit ``date``.
EARLY_CREATED_AT = "2020-01-01T00:00:00Z"


def test_probe_grep_clause_e2e(probe_module, tmp_path):
    """Grep clause on a fixture worktree resolves the introducing PR.

    Mirrors the canonical #4472 case: the AC's literal Verify clause is
    a grep for a content invariant, the worktree contains a file with
    the matching content, and the introducing commit's subject ends in
    ``(#<n>)``. The probe must return that PR number.
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="WIP: ralph output (#3215)",
        files={
            "scripts/dispatcher/tests/test_argv_allowlist.py": (
                "ALLOWED_CLAUDE_FLAGS = frozenset({'-p', '--max-turns', '--model'})\n"
            ),
        },
    )
    body = (
        "- [ ] Allowlist exists.\n"
        '  **Verify:** `grep "ALLOWED_CLAUDE_FLAGS" scripts/dispatcher/tests/`'
        " returns a match.\n"
    )
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is not None
    pr_num, clause = hit
    assert pr_num == 3215
    assert "ALLOWED_CLAUDE_FLAGS" in clause


def test_probe_grep_clause_no_match_returns_none(probe_module, tmp_path):
    """When the worktree has no match for the grep pattern, probe misses."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="initial (#1)",
        files={"scripts/foo.sh": "echo hello\n"},
    )
    body = "  Verify: grep ABSENT_TOKEN scripts/foo.sh\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_probe_pytest_clause_e2e(probe_module, tmp_path):
    """Pytest clause on a fixture worktree resolves the introducing PR.

    The fixture commits a single test file with a parametrized test name;
    the probe runs ``pytest --collect-only -q -k <name>`` against the
    fixture, observes a collected test, and resolves the PR via
    pickaxe search on the test name.
    """
    repo = _git_init_repo(tmp_path)
    test_body = (
        "def test_phase_constants_cover_all_declared_phases():\n    assert True\n"
    )
    _git_commit(
        repo,
        message="test(dispatcher): cover all declared phases (#3253)",
        files={"scripts/dispatcher/tests/test_phase_constants.py": test_body},
    )
    body = (
        "- [ ] Test exists.\n"
        "  Verify: pytest -k test_phase_constants_cover_all_declared_phases\n"
    )
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=30, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is not None
    pr_num, clause = hit
    assert pr_num == 3253
    # The canonical clause carries the rewritten --collect-only flag (AC4).
    assert "--collect-only" in clause


def test_probe_pytest_clause_no_match_returns_none(probe_module, tmp_path):
    """Pytest probe with no test matching ``-k`` expr returns None."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="initial (#1)",
        files={
            "scripts/dispatcher/tests/test_other.py": "def test_other():\n    assert True\n"
        },
    )
    body = "  Verify: pytest -k test_absent_test_name_definitely_not_present\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_probe_script_clause_drops_silently(probe_module, tmp_path):
    """Script-execution Verify clauses must NOT match — even when the script exists.

    Issue #4472 design decision: the verify-channel does not support
    script-execution clauses. A body whose only Verify clause is a
    ``./scripts/foo.sh`` invocation — even when the script exists in
    the worktree and was added by a prior PR — must miss the verify-
    channel and fall through to the path-overlap channel. This locks in
    the safety + precision rationale documented in the helper's
    "DELIBERATELY UNSUPPORTED" header.

    Concrete failure mode this prevents: when /task picks up an
    AC-extension issue (e.g. issue #4472 itself, asking to extend
    ``scripts/check-shipped-pr.sh``), the script EXISTS but the
    extension hasn't shipped. A naive script-existence probe would
    false-positive against the script's introducing PR.
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="dx: add foo helper (#4001)",
        files={"scripts/foo-helper.sh": "#!/usr/bin/env bash\necho hi\n"},
    )
    body = "  Verify: ./scripts/foo-helper.sh exits 0\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_probe_first_clause_wins(probe_module, tmp_path):
    """When multiple Verify clauses match, the first one in source order fires."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="first (#100)",
        files={
            "scripts/a.sh": "FIRST_TOKEN_HERE\n",
            "scripts/b.sh": "SECOND_TOKEN_HERE\n",
        },
    )
    body = (
        "- [ ] A.\n"
        "  Verify: grep FIRST_TOKEN_HERE scripts/a.sh\n"
        "- [ ] B.\n"
        "  Verify: grep SECOND_TOKEN_HERE scripts/b.sh\n"
    )
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is not None
    pr_num, clause = hit
    # Either clause would resolve to PR #100 (single commit), so we
    # check the clause text to distinguish: the first one in source
    # order should win.
    assert pr_num == 100
    assert "FIRST_TOKEN_HERE" in clause


def test_probe_unsupported_clause_silently_skipped(probe_module, tmp_path):
    """Unsupported clauses (aws / curl / sql / prose) don't trip the probe.

    AC5: an issue body whose Verify clauses are all unsupported shapes
    must miss — the wrapper falls through to the path-overlap channel.
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="initial (#1)",
        files={"scripts/foo.sh": "WIDGET_TOKEN\n"},
    )
    body = (
        "- [ ] A.\n"
        "  Verify: aws ecs describe-clusters --include SETTINGS\n"
        "- [ ] B.\n"
        "  Verify: SELECT COUNT(*) FROM derived.rulings\n"
        "- [ ] C.\n"
        "  Verify: reviewer confirms on read-through\n"
    )
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_probe_empty_body_returns_none(probe_module, tmp_path):
    """Empty body → no clauses → no match."""
    repo = _git_init_repo(tmp_path)
    hit = probe_module.probe(
        "", repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


# ─── stdin-driven main entrypoint ─────────────────────────────────────────


def test_main_emits_shipped_line_on_match(probe_module, tmp_path, monkeypatch, capsys):
    """The CLI entrypoint emits ``shipped:<pr>\\t<clause>`` on a match."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="add helper (#9001)",
        files={"scripts/helper.sh": "WIDGET_TOKEN\n"},
    )
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_REPO_ROOT", str(repo))
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_TIMEOUT_SEC", "15")

    issue_json = (
        '{"body": "- [ ] Foo.\\n  Verify: grep WIDGET_TOKEN scripts/helper.sh\\n", '
        '"title": "feat: foo", "createdAt": "2020-01-01T00:00:00Z"}'
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        type("S", (), {"read": staticmethod(lambda: issue_json)})(),
    )
    # json.load wants a file-like object — patch it to a stringio-ish
    # wrapper.
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(issue_json))

    rc = probe_module.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("shipped:9001\t")
    assert "WIDGET_TOKEN" in out


def test_main_returns_1_on_no_match(probe_module, tmp_path, monkeypatch):
    """The CLI returns exit 1 with empty stdout on no Verify-channel match."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="initial (#1)",
        files={"scripts/foo.sh": "echo hi\n"},
    )
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_REPO_ROOT", str(repo))
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_TIMEOUT_SEC", "15")

    import io

    issue_json = '{"body": "Pure prose with no Verify clause.", "title": "x"}'
    monkeypatch.setattr(sys, "stdin", io.StringIO(issue_json))

    rc = probe_module.main()
    assert rc == 1


def test_main_returns_2_on_malformed_json(probe_module, monkeypatch):
    """The CLI returns exit 2 on malformed JSON input."""
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    rc = probe_module.main()
    assert rc == 2


def test_main_returns_1_on_empty_body(probe_module, monkeypatch):
    """A JSON object with empty body returns exit 1 (no match), not exit 2."""
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"body": "", "title": "x"}'))
    rc = probe_module.main()
    assert rc == 1


# ─── Fail-safe guards (#4666) ─────────────────────────────────────────────
#
# Regression for #4666: on issue #4661 (an open p1 data-loss bug) the probe
# reported ``shipped: PR #4324`` because the AC's grep
# (``grep -n "capture_timestamp" scripts/rebuild_db.py``) matched the BUGGY
# line itself, and the unscoped pickaxe fallback then attributed the hit to
# an unrelated earlier PR that never touched rebuild_db.py. A grep hit alone
# cannot tell "the grep target exists" from "the fix exists", so the probe
# must require evidence of the fix and default to "not shipped".

# The literal #4661 body (trimmed to the load-bearing sections). The AC's
# Verify prose is a qualitative/negative assertion ("sourced from ..., not
# `datetime.now`") that a bare grep hit cannot confirm.
ISSUE_4661_BODY = (
    "## Summary\n\n"
    "`scripts/rebuild_db.py` stamps every rebuilt document with "
    "`capture_timestamp = datetime.now(UTC)` (`scripts/rebuild_db.py:310`) "
    "instead of the document's original capture time.\n\n"
    "## Acceptance criteria\n\n"
    "1. `rebuild_db.py` no longer sets `capture_timestamp` to `now()`.\n"
    '   - Verify: `grep -n "capture_timestamp" scripts/rebuild_db.py` shows it '
    "sourced from S3/capture metadata, not `datetime.now`.\n"
    "2. Regression test covers a document older than 180 days surviving a "
    "rebuild.\n"
    "   - Verify: the new test fails on the pre-fix code and passes after.\n"
)
ISSUE_4661_CREATED_AT = "2026-09-24T21:57:13Z"

# Pre-fix rebuild_db.py content: the buggy line is what the grep matches.
BUGGY_REBUILD_DB = (
    "from datetime import UTC, datetime\n\n"
    "def build_event(key):\n"
    '    return {"key": key, "capture_timestamp": datetime.now(UTC).isoformat()}\n'
)


def _build_4661_fixture(tmp_path: Path) -> Path:
    """Reproduce the #4661 / #4324 repo state.

    - rebuild_db.py (with the buggy ``capture_timestamp`` line) was added by
      a commit with NO ``(#N)`` squash-merge token, so the path-scoped
      pickaxe finds no PR.
    - PR #4324 (merged months before #4661 was filed) touched a DIFFERENT
      file that also mentions ``capture_timestamp`` — the unscoped pickaxe
      fallback picked it up and attributed the issue to it.
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="feat(schema): regenerate schema.sql, add rebuild",
        files={"scripts/rebuild_db.py": BUGGY_REBUILD_DB},
        date="2026-03-28T16:38:52-07:00",
    )
    _git_commit(
        repo,
        message="fix(ingestion): unrelated capture_timestamp plumbing (#4324)",
        files={
            "packages/scraper-framework/src/ingestion/other.py": (
                "capture_timestamp = None\n"
            )
        },
        date="2026-05-09T12:00:00-07:00",
    )
    return repo


def test_4661_shape_does_not_report_shipped(probe_module, tmp_path):
    """#4661 body + #4324 fixture → probe must miss (AC1 of #4666)."""
    repo = _build_4661_fixture(tmp_path)
    hit = probe_module.probe(
        ISSUE_4661_BODY,
        repo_root=repo,
        timeout_sec=15,
        issue_created_at=ISSUE_4661_CREATED_AT,
    )
    assert hit is None


def test_4661_main_exits_1(probe_module, tmp_path, monkeypatch, capsys):
    """The CLI on the #4661 shape exits 1 with empty stdout."""
    import io
    import json

    repo = _build_4661_fixture(tmp_path)
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_REPO_ROOT", str(repo))
    monkeypatch.setenv("CHECK_SHIPPED_VERIFY_TIMEOUT_SEC", "15")
    issue_json = json.dumps(
        {
            "body": ISSUE_4661_BODY,
            "title": "fix(ingestion): rebuild_db.py stamps capture_timestamp=now",
            "createdAt": ISSUE_4661_CREATED_AT,
        }
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(issue_json))
    assert probe_module.main() == 1
    assert capsys.readouterr().out == ""


def test_pr_resolution_is_scoped_to_matched_files(probe_module, tmp_path):
    """No unscoped pickaxe fallback: a PR that never touched the grep-matched
    file cannot be credited with it, even with a bare-existence Verify line
    and a post-issue merge date."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="initial import without PR token",
        files={"scripts/target.py": "WIDGET_TOKEN = 1\n"},
    )
    _git_commit(
        repo,
        message="unrelated (#777)",
        files={"scripts/elsewhere.py": "WIDGET_TOKEN = 2\n"},
    )
    body = "  Verify: `grep WIDGET_TOKEN scripts/target.py` returns a match\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_pr_merged_before_issue_created_does_not_match(probe_module, tmp_path):
    """Date-ordering guard: the introducing PR predates the issue → miss.

    A PR merged before the issue existed cannot have fixed it — the grep
    target was already present when the issue was filed (i.e. the grep
    matches the pre-fix state).
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="add widget (#100)",
        files={"scripts/widget.py": "WIDGET_TOKEN = 1\n"},
        date="2026-01-01T00:00:00Z",
    )
    body = "  Verify: `grep WIDGET_TOKEN scripts/widget.py` returns a match\n"
    hit = probe_module.probe(
        body,
        repo_root=repo,
        timeout_sec=15,
        issue_created_at="2026-02-01T00:00:00Z",
    )
    assert hit is None


def test_pr_merged_after_issue_created_matches(probe_module, tmp_path):
    """Sanity: a post-issue PR with a bare-existence Verify line still fires."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="add widget (#200)",
        files={"scripts/widget.py": "WIDGET_TOKEN = 1\n"},
        date="2026-03-01T00:00:00Z",
    )
    body = "  Verify: `grep WIDGET_TOKEN scripts/widget.py` returns a match\n"
    hit = probe_module.probe(
        body,
        repo_root=repo,
        timeout_sec=15,
        issue_created_at="2026-02-01T00:00:00Z",
    )
    assert hit is not None
    assert hit[0] == 200


def test_missing_created_at_defaults_to_not_shipped(probe_module, tmp_path):
    """No issue createdAt → the date guard can't be applied → miss (fail safe)."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="add widget (#200)",
        files={"scripts/widget.py": "WIDGET_TOKEN = 1\n"},
    )
    body = "  Verify: `grep WIDGET_TOKEN scripts/widget.py` returns a match\n"
    for created_at in (None, "", "not-a-date"):
        hit = probe_module.probe(
            body, repo_root=repo, timeout_sec=15, issue_created_at=created_at
        )
        assert hit is None


@pytest.mark.parametrize(
    "prose",
    [
        "shows it sourced from S3/capture metadata, not `datetime.now`.",
        "returns no matches",
        "no longer matches",
        "returns nothing",
        "shows the new helper is used instead of the old one",
        "shows only the fixed call site",
    ],
)
def test_grep_with_qualitative_or_negative_prose_is_skipped(
    probe_module, tmp_path, prose
):
    """A grep whose AC prose asserts more than "a match exists" is skipped.

    A bare hit cannot distinguish the fixed state from the bug for
    negative ("not X", "no matches") or qualitative ("shows it sourced
    from ...") assertions — even when the PR post-dates the issue.
    """
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="add widget (#200)",
        files={"scripts/widget.py": "WIDGET_TOKEN = 1\n"},
    )
    body = f"  Verify: `grep WIDGET_TOKEN scripts/widget.py` {prose}\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=15, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


@pytest.mark.parametrize(
    "prose",
    [
        "",
        "returns a match.",
        "returns a match",
        "returns at least one match",
        "has a hit",
        "matches",
        "finds it",
        "is non-empty",
        "exits 0",
    ],
)
def test_prose_is_bare_existence_accepts(probe_module, prose):
    """Bare-existence phrasings are the only ones a grep hit can confirm."""
    assert probe_module._prose_is_bare_existence(prose)


def test_pytest_with_negative_prose_is_skipped(probe_module, tmp_path):
    """``pytest -k X`` whose prose says it must NOT collect is skipped."""
    repo = _git_init_repo(tmp_path)
    _git_commit(
        repo,
        message="test: add legacy test (#300)",
        files={"tests/test_legacy.py": "def test_legacy_path():\n    assert True\n"},
    )
    body = "  Verify: `pytest -k test_legacy_path` no longer collects anything\n"
    hit = probe_module.probe(
        body, repo_root=repo, timeout_sec=30, issue_created_at=EARLY_CREATED_AT
    )
    assert hit is None


def test_extract_verify_entries_keeps_prose(probe_module):
    """The entry extractor returns (command, trailing prose) pairs."""
    body = (
        '  - Verify: `grep -n "capture_timestamp" scripts/rebuild_db.py` shows it '
        "sourced from S3, not `datetime.now`.\n"
        "  Verify: grep foo bar/\n"
    )
    entries = probe_module._extract_verify_entries(body)
    assert entries[0][0] == 'grep -n "capture_timestamp" scripts/rebuild_db.py'
    assert "not `datetime.now`" in entries[0][1]
    assert entries[1] == ("grep foo bar/", "")
