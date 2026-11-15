#!/usr/bin/env python3
# venv: none
# permanent: true
# ci-guards: skip
# (run-ci-guards.sh skips it: it needs a PR number, or a draft title/body.)
"""check-pr-closing-refs.py — Fail when a PR would close an issue it is not
meant to close (#5071).

Why this check exists
---------------------
GitHub closes an issue when a closing keyword (close, closes, closed, fix,
fixes, fixed, resolve, resolves, resolved) sits directly before its number:
in a PR description, the issue closes when the PR merges; in a commit
message, when the commit lands on main. Prose that describes an incident
trips it by accident. PR #5072's description said "My local run closed
#4673", and merging it re-closed #4673, an open, blocked issue.

What it checks
--------------
The issues the PR would close are:

* its ``closingIssuesReferences`` (GitHub's own parse of the body, plus any
  issue linked in the PR's Development sidebar), with ``--pr``;
* every closing keyword + ``#N`` in the body (textual parse, same grammar);
* every closing keyword + ``#N`` in the PR's commit messages. The repo
  squash-merges with the commit messages as the squash body, so these land
  on main.

Each one must be an issue the PR is meant to close:

* a number in the branch name (``agent/5071-...``, ``fix-5071``);
* a ``#N`` in the PR title (``fix(x): ... (#5071)``);
* a ``#N`` on an explicit closing line in the body: a line made only of
  closing statements, such as the ``Closes #N`` line /task writes at the
  end of every PR body.

Anything else fails with a Fix: block telling the author how to reword.
Only refs to this repository count (``#N``, ``owner/repo#N`` or an issue
URL for ``--repo``).

CLI
---
::

    # CI: check an open PR (title, body, branch, closing refs, commits via gh)
    scripts/check-pr-closing-refs.py --pr 5080

    # Before opening a PR: check a draft body (and the branch's commits)
    scripts/check-pr-closing-refs.py --title "fix(x): y (#5071)" \\
        --body-file tmp/pr_body.txt --git-range origin/main..HEAD

``--branch`` defaults to the current git branch in draft mode.

Exit codes
----------
* ``0`` — every issue the PR would close is one it is meant to close.
* ``1`` — the PR would close at least one other issue (Fix: block on stderr).
* ``2`` — usage error, or the PR could not be read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPO = "judgemind/judgemind"

_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A reference: #N, owner/repo#N, or a full issue URL.
_REF = (
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_num>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+))"
)
# GitHub's grammar: keyword, optional colon, whitespace, reference. The
# keyword must not be glued to a preceding word ("autoclosed #5" does not
# close; "auto-closed #5" does, since GitHub splits on the hyphen).
CLOSING_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){_KEYWORD}:?\s+{_REF}(?![A-Za-z0-9_])", re.IGNORECASE
)
# Separators allowed between closing statements on an explicit closing line.
_CLOSING_LINE_FILLER_RE = re.compile(
    r"^[\s,.;:*>+-]*(?:and[\s,.;:*>+-]*)*$", re.IGNORECASE
)
_TITLE_REF_RE = re.compile(r"#(\d+)\b")
_BRANCH_NUM_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)(?![A-Za-z0-9])")


@dataclass
class Hit:
    """One place that would close an issue."""

    number: int
    source: str
    context: str


@dataclass
class Verdict:
    allowed: set[int] = field(default_factory=set)
    hits: list[Hit] = field(default_factory=list)

    @property
    def unexpected(self) -> list[Hit]:
        return [h for h in self.hits if h.number not in self.allowed]


def _ref_number(match: re.Match[str], repo: str) -> int | None:
    """The issue number a CLOSING_RE match points at, or None for another repo."""
    ref_repo = match.group("url_repo") or match.group("repo")
    if ref_repo and ref_repo.lower() != repo.lower():
        return None
    return int(match.group("url_num") or match.group("num"))


def closing_refs(text: str, repo: str = DEFAULT_REPO) -> list[tuple[int, str]]:
    """Every (issue number, line) a closing keyword in ``text`` points at."""
    found: list[tuple[int, str]] = []
    for line in text.splitlines():
        for m in CLOSING_RE.finditer(line):
            number = _ref_number(m, repo)
            if number is not None:
                found.append((number, line.strip()))
    return found


def explicit_closing_numbers(body: str, repo: str = DEFAULT_REPO) -> set[int]:
    """Numbers on body lines made only of closing statements ("Closes #N")."""
    numbers: set[int] = set()
    for line in body.splitlines():
        matches = list(CLOSING_RE.finditer(line))
        if not matches:
            continue
        if not _CLOSING_LINE_FILLER_RE.match(CLOSING_RE.sub("", line)):
            continue  # prose around the keyword: not an explicit closing line
        for m in matches:
            number = _ref_number(m, repo)
            if number is not None:
                numbers.add(number)
    return numbers


def allowed_numbers(
    branch: str, title: str, body: str, repo: str = DEFAULT_REPO
) -> set[int]:
    """Issues the PR is meant to close: branch numbers, title refs, explicit lines."""
    allowed = {int(n) for n in _BRANCH_NUM_RE.findall(branch or "")}
    allowed |= {int(n) for n in _TITLE_REF_RE.findall(title or "")}
    allowed |= explicit_closing_numbers(body or "", repo)
    return allowed


def evaluate(
    *,
    branch: str,
    title: str,
    body: str,
    commit_messages: list[str] | None = None,
    linked: list[int] | None = None,
    repo: str = DEFAULT_REPO,
) -> Verdict:
    """Compare what the PR would close against what it is meant to close."""
    verdict = Verdict(allowed=allowed_numbers(branch, title, body, repo))
    textual_body = closing_refs(body or "", repo)
    for number, line in textual_body:
        verdict.hits.append(Hit(number, "PR body", line))
    seen_in_body = {n for n, _ in textual_body}
    for number in linked or []:
        if number not in seen_in_body:
            verdict.hits.append(
                Hit(
                    number,
                    "closingIssuesReferences",
                    "(linked in the Development sidebar)",
                )
            )
    for message in commit_messages or []:
        subject = message.strip().splitlines()[0] if message.strip() else ""
        for number, line in closing_refs(message, repo):
            verdict.hits.append(Hit(number, f"commit message ({subject[:60]})", line))
    return verdict


def format_failure(verdict: Verdict, label: str) -> str:
    """The FAIL report plus Fix: block for the unexpected hits."""
    bad = verdict.unexpected
    numbers = sorted({h.number for h in bad})
    meant = ", ".join(f"#{n}" for n in sorted(verdict.allowed)) or "(none found)"
    lines = [
        f"FAIL: {label} would close issue(s) it is not meant to close (#5071): "
        + ", ".join(f"#{n}" for n in numbers),
    ]
    for hit in bad:
        lines.append(f"  - #{hit.number} via {hit.source}: {hit.context[:160]}")
    lines += [
        f"  Meant to close: {meant} (branch name, PR title, or a 'Closes #N' line).",
        "",
        "Fix:",
        "  Reword each line above so no closing word (close/closes/closed, fix/fixes/fixed,",
        "  resolve/resolves/resolved) sits directly before an issue number. Drop the '#'",
        "  or put a word in between, e.g.:",
        '    "my local run closed #4673"  ->  "my local run auto-closed issue 4673"',
        '    "fixes #123 too"             ->  "also addresses issue 123 (see #123)"',
    ]
    if any(h.source == "PR body" for h in bad):
        lines.append("  PR body: gh pr edit <PR> --body-file <reworded-body-file>")
    if any(h.source.startswith("commit message") for h in bad):
        lines.append(
            "  Commit message (the squash merge copies it to main): "
            "git commit --amend -F <file> for the last commit,"
        )
        lines.append(
            "    or git reset --soft origin/main then one new commit; "
            "then git push --force-with-lease."
        )
    if any(h.source == "closingIssuesReferences" for h in bad):
        lines.append("  Linked issue: unlink it in the PR's Development sidebar.")
    lines.append(
        "  If the PR really is meant to close one of them, add a line 'Closes #N' "
        "at the end of the body."
    )
    return "\n".join(lines)


def _gh_json(args: list[str]) -> object:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      title
      body
      headRefName
      closingIssuesReferences(first: 50) {
        nodes { number repository { nameWithOwner } }
      }
      commits(first: 250) { nodes { commit { message } } }
    }
  }
}
"""


def fetch_pr(pr: int, repo: str) -> dict[str, object]:
    """Title, body, branch, linked closing refs and commit messages of a PR."""
    owner, name = repo.split("/", 1)
    data = _gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"query={_PR_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr}",
        ]
    )
    node = data["data"]["repository"]["pullRequest"]  # type: ignore[index]
    linked = [
        n["number"]
        for n in node["closingIssuesReferences"]["nodes"]
        if n["repository"]["nameWithOwner"].lower() == repo.lower()
    ]
    return {
        "title": node["title"] or "",
        "body": node["body"] or "",
        "branch": node["headRefName"] or "",
        "linked": linked,
        "commits": [c["commit"]["message"] for c in node["commits"]["nodes"]],
    }


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pr", type=int, help="PR number to fetch with gh")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--title", help="draft PR title")
    parser.add_argument("--body-file", type=Path, help="draft PR body file")
    parser.add_argument(
        "--branch", help="head branch (draft mode default: current branch)"
    )
    parser.add_argument(
        "--git-range",
        help="also check commit messages in this range, e.g. origin/main..HEAD",
    )
    args = parser.parse_args(argv)

    try:
        if args.pr is not None:
            pr = fetch_pr(args.pr, args.repo)
            label = f"PR #{args.pr}"
            verdict = evaluate(
                branch=pr["branch"],  # type: ignore[arg-type]
                title=pr["title"],  # type: ignore[arg-type]
                body=pr["body"],  # type: ignore[arg-type]
                commit_messages=pr["commits"],  # type: ignore[arg-type]
                linked=pr["linked"],  # type: ignore[arg-type]
                repo=args.repo,
            )
        else:
            if args.title is None and args.body_file is None:
                parser.error("pass --pr N, or --title and/or --body-file for a draft")
            body = args.body_file.read_text() if args.body_file else ""
            branch = args.branch
            if branch is None:
                branch = _git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
            commits: list[str] = []
            if args.git_range:
                raw = _git(["log", "--format=%B%x00", args.git_range])
                commits = [m for m in raw.split("\x00") if m.strip()]
            label = "this draft PR"
            verdict = evaluate(
                branch=branch,
                title=args.title or "",
                body=body,
                commit_messages=commits,
                repo=args.repo,
            )
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"ERROR: check-pr-closing-refs: {exc}", file=sys.stderr)
        return 2

    if verdict.unexpected:
        print(format_failure(verdict, label), file=sys.stderr)
        return 1
    closes = sorted({h.number for h in verdict.hits})
    summary = ", ".join(f"#{n}" for n in closes) or "no issues"
    print(f"OK: {label} closes {summary}; every one is meant (#5071)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
