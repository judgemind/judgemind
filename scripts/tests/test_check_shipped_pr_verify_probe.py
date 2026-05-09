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


def _git_commit(repo: Path, *, message: str, files: dict[str, str]) -> None:
    """Write ``files`` and commit them with ``message``."""
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "add", rel_path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=repo, check=True)


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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=30)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
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
    hit = probe_module.probe(body, repo_root=repo, timeout_sec=15)
    assert hit is None


def test_probe_empty_body_returns_none(probe_module, tmp_path):
    """Empty body → no clauses → no match."""
    repo = _git_init_repo(tmp_path)
    hit = probe_module.probe("", repo_root=repo, timeout_sec=15)
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
        '"title": "feat: foo"}'
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
