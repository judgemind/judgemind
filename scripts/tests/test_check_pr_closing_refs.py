"""Tests for scripts/check-pr-closing-refs.py (#5071).

PR #5072's description said "My local run closed #4673"; GitHub read it as a
closing keyword and merging re-closed #4673. The check must fail on that
body, pass once it is reworded, and accept the PR's own ``Closes #N`` line.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check-pr-closing-refs.py"
_spec = importlib.util.spec_from_file_location("check_pr_closing_refs", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod  # dataclasses look the module up by name
_spec.loader.exec_module(mod)

INCIDENT_BODY = """\
## Summary

Scrub GIT_DIR in every test root. My local run closed #4673 while I was
verifying, so I reopened it.

Closes #5058
"""

REWORDED_BODY = INCIDENT_BODY.replace(
    "My local run closed #4673", "My local run auto-closed issue 4673"
)


# ── closing_refs: GitHub's keyword grammar ───────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Closes #12", [12]),
        ("closed #12", [12]),
        ("close #12", [12]),
        ("Fixes #12", [12]),
        ("fixed #12", [12]),
        ("fix #12", [12]),
        ("Resolves #12", [12]),
        ("resolved #12", [12]),
        ("resolve #12", [12]),
        ("Closes: #12", [12]),
        ("CLOSES #12", [12]),
        ("closes judgemind/judgemind#12", [12]),
        ("closes https://github.com/judgemind/judgemind/issues/12", [12]),
        ("the run auto-closed #12", [12]),
        ("fixes #12, fixes #13", [12, 13]),
        # Not closing references:
        ("closes other/repo#12", []),
        ("the run auto-closed issue 12", []),
        ("closed (#12)", []),
        ("closed issue #12", []),
        ("autoclosed #12", []),
        ("see #12", []),
        ("closes #12abc", []),
        ("prefixes #12", []),
    ],
)
def test_closing_refs_grammar(text: str, expected: list[int]) -> None:
    assert [n for n, _ in mod.closing_refs(text)] == expected


# ── allowed_numbers: what the PR is meant to close ───────────────────────


def test_allowed_from_branch_title_and_explicit_line() -> None:
    allowed = mod.allowed_numbers(
        "agent/5071-gh-isolate", "fix(test): thing (#5080)", "text\n\nCloses #5090\n"
    )
    assert allowed == {5071, 5080, 5090}


def test_hex_worktree_branch_contributes_no_numbers() -> None:
    assert mod.allowed_numbers("worktree-agent-acaa0b22f6ed2ff22", "", "") == set()


@pytest.mark.parametrize(
    "line",
    [
        "Closes #7",
        "Closes #7, closes #8",
        "Fixes #7 and fixes #8.",
        "Resolves: #7",
        "  closes #7  ",
        "CLOSES #7",
        "fix: #7",
    ],
)
def test_explicit_closing_lines(line: str) -> None:
    assert 7 in mod.explicit_closing_numbers(line)


def test_prose_line_is_not_explicit() -> None:
    assert mod.explicit_closing_numbers("My local run closed #4673 by mistake") == set()


# #5090: a list or quote prefix, or the past tense, reads as incident history.
@pytest.mark.parametrize(
    "line",
    [
        "- Fixed #7",
        "- Resolves: #7",
        "* Closes #7",
        "+ fixes #7",
        "> closes #7",
        "  - closes #7",
        "1. Fixes #7",
        "**Closes #7**",
        "Fixed #7",
        "Closed #7.",
        "Closes #8, fixed #7",
        "and closes #7",
    ],
)
def test_bulleted_quoted_or_past_tense_lines_are_not_explicit(line: str) -> None:
    assert 7 not in mod.explicit_closing_numbers(line)


def test_bulleted_fixed_line_in_body_is_unexpected() -> None:
    body = (
        "## Summary\n\nIncident history:\n\n- Fixed #4242\n"
        "- the retry loop\n\nCloses #5090\n"
    )
    verdict = mod.evaluate(branch="worktree-agent-x", title="fix(ci): x", body=body)
    assert [(h.number, h.source) for h in verdict.unexpected] == [(4242, "PR body")]
    assert verdict.allowed == {5090}


def test_bulleted_fixed_line_fails_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = tmp_path / "body.md"
    body.write_text("- Fixed #4242\n\nCloses #5090\n")
    rc = mod.main(["--title", "fix(ci): x", "--body-file", str(body), "--branch", "wt"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "#4242 via PR body: - Fixed #4242" in err
    assert "no '-', '*' or '>' prefix" in err


# ── evaluate: the #5072 incident and its fix ─────────────────────────────


def test_incident_body_fails() -> None:
    verdict = mod.evaluate(
        branch="worktree-agent-x", title="fix(test): x (#5058)", body=INCIDENT_BODY
    )
    assert [h.number for h in verdict.unexpected] == [4673]
    assert verdict.unexpected[0].source == "PR body"


def test_reworded_body_passes() -> None:
    verdict = mod.evaluate(
        branch="worktree-agent-x", title="fix(test): x (#5058)", body=REWORDED_BODY
    )
    assert verdict.unexpected == []
    assert {h.number for h in verdict.hits} == {5058}


def test_linked_issue_not_meant_fails() -> None:
    verdict = mod.evaluate(branch="b", title="x (#1)", body="Closes #1", linked=[1, 99])
    assert [(h.number, h.source) for h in verdict.unexpected] == [
        (99, "closingIssuesReferences")
    ]


def test_linked_issue_already_in_body_is_reported_once() -> None:
    verdict = mod.evaluate(branch="b", title="x", body="I fixed #99 here", linked=[99])
    assert [h.number for h in verdict.unexpected] == [99]


def test_commit_message_closing_other_issue_fails() -> None:
    verdict = mod.evaluate(
        branch="agent/5071",
        title="x (#5071)",
        body="Closes #5071",
        commit_messages=["fix(x): y (#5071)\n\nThis also fixes #12.\n"],
    )
    assert [h.number for h in verdict.unexpected] == [12]
    assert verdict.unexpected[0].source.startswith("commit message (fix(x): y (#5071)")


def test_closing_ref_in_title_is_meant() -> None:
    verdict = mod.evaluate(
        branch="b", title="fix: closes #3", body="Fixes #3 in passing"
    )
    assert verdict.unexpected == []


# ── format_failure: Fix block ────────────────────────────────────────────


def test_failure_report_has_fix_block() -> None:
    verdict = mod.evaluate(
        branch="b",
        title="x (#5058)",
        body=INCIDENT_BODY,
        commit_messages=["c\n\nfixes #12"],
        linked=[77],
    )
    report = mod.format_failure(verdict, "PR #5072")
    assert report.startswith(
        "FAIL: PR #5072 would close issue(s) it is not meant to close"
    )
    assert "#12, #77, #4673" in report.splitlines()[0]
    assert "Fix:" in report
    assert "auto-closed issue 4673" in report
    assert "gh pr edit <PR> --body-file" in report
    # #5078: wait-for-ci.sh ignores a failure a newer run replaced, so the
    # Fix: block must not send the author to rerun the failed run.
    assert "gh run rerun" not in report
    assert "no rerun" in report
    assert "git commit --amend" in report
    assert "Development sidebar" in report
    assert "Meant to close: #5058" in report


# ── main: draft mode and --pr mode ───────────────────────────────────────


def test_main_draft_mode_fails_then_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = tmp_path / "body.md"
    body.write_text(INCIDENT_BODY)
    rc = mod.main(
        ["--title", "fix(test): x (#5058)", "--body-file", str(body), "--branch", "wt"]
    )
    assert rc == 1
    assert "#4673" in capsys.readouterr().err

    body.write_text(REWORDED_BODY)
    rc = mod.main(
        ["--title", "fix(test): x (#5058)", "--body-file", str(body), "--branch", "wt"]
    )
    assert rc == 0
    assert "OK: this draft PR closes #5058" in capsys.readouterr().out


def test_main_draft_mode_needs_input() -> None:
    with pytest.raises(SystemExit) as exc:
        mod.main(["--branch", "b"])
    assert exc.value.code == 2


def test_main_draft_mode_reads_git_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake_git(args: list[str]) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return "agent/5071-x\n"
        return "fix: a (#5071)\n\nalso resolves #8\n\x00\nfix: b\n\x00\n"

    monkeypatch.setattr(mod, "_git", fake_git)
    body = tmp_path / "body.md"
    body.write_text("Closes #5071\n")
    rc = mod.main(["--body-file", str(body), "--git-range", "origin/main..HEAD"])
    assert rc == 1
    assert "#8 via commit message" in capsys.readouterr().err
    assert ["log", "--format=%B%x00", "origin/main..HEAD"] in calls


def _graphql_payload(**overrides: Any) -> str:
    node = {
        "title": "fix(test): x (#5058)",
        "body": INCIDENT_BODY,
        "headRefName": "worktree-agent-x",
        "closingIssuesReferences": {
            "nodes": [
                {
                    "number": 5058,
                    "repository": {"nameWithOwner": "judgemind/judgemind"},
                },
                {
                    "number": 4673,
                    "repository": {"nameWithOwner": "judgemind/judgemind"},
                },
                {"number": 1, "repository": {"nameWithOwner": "other/repo"}},
            ]
        },
        "commits": {"nodes": [{"commit": {"message": "fix(test): x (#5058)"}}]},
    }
    node.update(overrides)
    return json.dumps({"data": {"repository": {"pullRequest": node}}})


def test_main_pr_mode_uses_graphql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, _graphql_payload(), "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main(["--pr", "5072"]) == 1
    err = capsys.readouterr().err
    assert "FAIL: PR #5072" in err
    assert "#4673 via PR body" in err
    assert seen[0][:3] == ["gh", "api", "graphql"]
    assert "number=5072" in seen[0]
    assert any("closingIssuesReferences" in part for part in seen[0])


def test_main_pr_mode_passes_after_rewording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _graphql_payload(
        body=REWORDED_BODY,
        closingIssuesReferences={
            "nodes": [
                {"number": 5058, "repository": {"nameWithOwner": "judgemind/judgemind"}}
            ]
        },
    )
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, payload, ""),
    )
    assert mod.main(["--pr", "5072"]) == 0
    assert "OK: PR #5072 closes #5058" in capsys.readouterr().out


def test_main_pr_mode_gh_failure_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **_: subprocess.CompletedProcess(cmd, 1, "", "HTTP 401"),
    )
    assert mod.main(["--pr", "1"]) == 2
    assert "HTTP 401" in capsys.readouterr().err


def test_main_cli_runs_as_script(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Closes #3\n")
    result = subprocess.run(
        [
            str(_SCRIPT_PATH),
            "--title",
            "x",
            "--body-file",
            str(body),
            "--branch",
            "issue-3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout
