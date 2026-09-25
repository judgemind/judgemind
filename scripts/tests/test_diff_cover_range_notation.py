# venv: none
"""Regression tests for the diff-coverage base range (#4764).

The diff-coverage gate must count only the PR's own lines. Before #4764
every diff-cover call site passed ``--diff-range-notation='..'``, which
diffs the tree of ``origin/main`` directly against ``HEAD``. When a commit
lands on main after the PR branched (or after GitHub computed the
``refs/pull/N/merge`` commit CI checks out), that commit's lines show up
in the "diff" and the gate fails on code the PR never touched. PR #4761
failed on ``cc_tentatives_portal.py`` lines from PR #4760 this way.

``'...'`` diffs from ``merge-base(origin/main, HEAD)`` instead, so later
main commits drop out.

Two layers of test:

* A scratch-git reproduction that runs real ``diff-cover`` with the
  notation the CI workflow uses. It covers both the CI shape (HEAD is a
  PR merge commit, main advanced afterwards) and the local pre-push shape
  (HEAD is the branch tip, ``origin/main`` fetched past the branch point).
* A static check that every diff-cover call site uses the merge-base
  notation, so the fix cannot regress at one site while the others stay
  correct.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every place that runs the diff-coverage gate. The ralph skill delegates
# to scripts/check-diff-coverage.sh; test_ralph_skill_delegates checks it.
CALL_SITES = [
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / ".githooks" / "pre-push",
    REPO_ROOT / "scripts" / "check-diff-coverage.sh",
]
RALPH_SKILL = REPO_ROOT / ".claude" / "skills" / "ralph" / "SKILL.md"

NOTATION_RE = re.compile(r"--diff-range-notation[= ]['\"]?([.]+)['\"]?")


def _ci_notation() -> str:
    """The range notation the CI coverage-check job passes to diff-cover."""
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    found = set(NOTATION_RE.findall(text))
    assert len(found) == 1, f"expected one notation in ci.yml, found {found}"
    return found.pop()


def _diff_cover_bin() -> str:
    found = os.environ.get("DIFF_COVER") or shutil.which("diff-cover")
    if found:
        return found
    if os.environ.get("CI"):
        pytest.fail(
            "diff-cover is not installed; the scripts-tests job must install it (#4764)"
        )
    pytest.skip("diff-cover not installed")


# ---------------------------------------------------------------------------
# Scratch-git reproduction
# ---------------------------------------------------------------------------

A_V1 = "def untouched():\n    return 1\n"
A_V2 = A_V1 + "\n\ndef added_by_pr(x):\n    y = x + 1\n    return y\n"
B_V1 = "def main_code(x):\n    if x:\n        return 1\n    return 2\n"
# The later main commit rewrites b.py, so the '..' diff counts b.py's
# old lines (which the PR never touched) as "added".
B_V2 = "def main_code(x):\n    if x > 0:\n        return 10\n    return 20\n"

# Coverage the PR's test run produced (from the PR's tree): the PR's new
# lines in a.py are covered, b.py's branch bodies are not.
COVERAGE_XML = """<?xml version="1.0" ?>
<coverage version="7.0" line-rate="0.5" branch-rate="0" lines-covered="6" lines-valid="10">
  <sources><source>{root}</source></sources>
  <packages>
    <package name="pkg" line-rate="0.5">
      <classes>
        <class name="a.py" filename="pkg/a.py" line-rate="1">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
            <line number="5" hits="1"/>
            <line number="6" hits="1"/>
            <line number="7" hits="1"/>
          </lines>
        </class>
        <class name="b.py" filename="pkg/b.py" line-rate="0.25">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
            <line number="3" hits="0"/>
            <line number="4" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> dict[str, object]:
    """main@A -> PR commit P; merge ref M=(A,P); main later advances to C.

    ``refs/remotes/origin/main`` points at C, as it does after the CI job's
    ``fetch-depth: 0`` checkout or a local ``git fetch origin main``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")

    base = _commit(repo, {"pkg/a.py": A_V1, "pkg/b.py": B_V1}, "A: base")

    _git(repo, "checkout", "-q", "-b", "pr")
    pr_tip = _commit(repo, {"pkg/a.py": A_V2}, "P: PR adds a function")

    # refs/pull/N/merge as GitHub computed it when the event fired.
    _git(repo, "checkout", "-q", "--detach", base)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M: merge ref", pr_tip)
    merge_ref = _git(repo, "rev-parse", "HEAD")

    # A different PR lands on main mid-run.
    _git(repo, "checkout", "-q", "main")
    advanced = _commit(repo, {"pkg/b.py": B_V2}, "C: another PR lands on main")
    _git(repo, "update-ref", "refs/remotes/origin/main", advanced)

    cov = tmp_path / "coverage.xml"
    cov.write_text(COVERAGE_XML.format(root=repo))
    return {"repo": repo, "cov": cov, "merge_ref": merge_ref, "pr_tip": pr_tip}


def _run_diff_cover(
    repo: Path, cov: Path, notation: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _diff_cover_bin(),
            str(cov),
            "--compare-branch=origin/main",
            "--fail-under=90",
            f"--diff-range-notation={notation}",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "head", ["merge_ref", "pr_tip"], ids=["ci-merge-ref", "local-branch-tip"]
)
def test_ci_notation_counts_only_the_prs_lines(
    scratch_repo: dict[str, object], head: str
) -> None:
    repo = scratch_repo["repo"]
    assert isinstance(repo, Path)
    _git(repo, "checkout", "-q", "--detach", str(scratch_repo[head]))

    result = _run_diff_cover(repo, scratch_repo["cov"], _ci_notation())  # type: ignore[arg-type]
    out = result.stdout + result.stderr

    assert result.returncode == 0, out
    assert "pkg/a.py" in out, out
    assert "pkg/b.py" not in out, f"main's later commit leaked into the PR diff:\n{out}"


@pytest.mark.parametrize(
    "head", ["merge_ref", "pr_tip"], ids=["ci-merge-ref", "local-branch-tip"]
)
def test_ci_notation_still_fails_uncovered_pr_lines(
    scratch_repo: dict[str, object], head: str
) -> None:
    """The gate keeps its strength: the PR's own uncovered lines still fail it."""
    repo = scratch_repo["repo"]
    cov = scratch_repo["cov"]
    assert isinstance(repo, Path) and isinstance(cov, Path)
    text = cov.read_text()
    for n in (5, 6, 7):
        text = text.replace(
            f'<line number="{n}" hits="1"/>', f'<line number="{n}" hits="0"/>'
        )
    cov.write_text(text)
    _git(repo, "checkout", "-q", "--detach", str(scratch_repo[head]))

    result = _run_diff_cover(repo, cov, _ci_notation())
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "pkg/a.py" in out and "Missing lines 5-7" in out, out
    assert "pkg/b.py" not in out, out


@pytest.mark.parametrize(
    "head", ["merge_ref", "pr_tip"], ids=["ci-merge-ref", "local-branch-tip"]
)
def test_two_dot_notation_reproduces_the_bug(
    scratch_repo: dict[str, object], head: str
) -> None:
    """Proves the fixture is sensitive: '..' flags b.py lines the PR never touched."""
    repo = scratch_repo["repo"]
    assert isinstance(repo, Path)
    _git(repo, "checkout", "-q", "--detach", str(scratch_repo[head]))

    result = _run_diff_cover(repo, scratch_repo["cov"], "..")  # type: ignore[arg-type]
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "pkg/b.py" in out, out


# ---------------------------------------------------------------------------
# Static: every call site uses the merge-base notation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", CALL_SITES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_call_site_uses_merge_base_notation(path: Path) -> None:
    text = path.read_text()
    invocations = [line for line in text.splitlines() if "--compare-branch" in line]
    assert invocations, f"no diff-cover --compare-branch call found in {path}"

    notations = NOTATION_RE.findall(text)
    assert len(notations) == len(invocations), (
        f"{path}: every diff-cover call must pass --diff-range-notation='...' explicitly "
        f"({len(invocations)} calls, {len(notations)} notations)"
    )
    assert set(notations) == {"..."}, (
        f"{path} uses --diff-range-notation {sorted(set(notations))}; only '...' "
        "(merge-base) keeps later main commits out of the PR's diff (#4764)"
    )


def test_ralph_skill_delegates_to_check_diff_coverage() -> None:
    """The ralph worker runs the shared script, not a hand-rolled diff-cover call."""
    text = RALPH_SKILL.read_text()
    assert "scripts/check-diff-coverage.sh" in text
    for line in text.splitlines():
        if "diff-cover" in line and "--compare-branch" in line:
            assert "--diff-range-notation='...'" in line, (
                f"ralph SKILL.md runs diff-cover without the merge-base notation:\n{line}"
            )


def test_coverage_check_fetches_full_history() -> None:
    """'...' needs the merge-base; a shallow clone has none and diff-cover errors."""
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = text.split("\n  coverage-check:\n", 1)[1].split("\n  # ----", 1)[0]
    assert "fetch-depth: 0" in job, "coverage-check must check out with fetch-depth: 0"
