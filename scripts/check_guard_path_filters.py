#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check_guard_path_filters.py — a guard must run on the PRs that touch the
code it guards.

Called by ``scripts/check-guard-path-filters.sh`` (the CI / pre-push entry
point).  Standard library only, so it runs on a bare ubuntu-latest runner and
in the pre-push hook's minimal environment.

Why (#5088)
───────────
``check-no-unguarded-s3-delete.sh`` scans ``packages/*/src``, ``infra/`` and
``.github/``; ``check-no-unflagged-page-cap.sh`` scans ``packages/*/src``.
Both ran only as steps inside ci.yml's ``scripts-tests`` job, whose ``if:``
gate is the ``scripts`` paths-filter (``scripts/**``, package tests, skills).
A PR that added an unguarded ``s3.delete_object`` under
``packages/scraper-framework/src/`` skipped ``scripts-tests``, so the guard
written for exactly that change never ran, and ``ci-passed`` went green.
Nothing checked that a guard's job trigger covers what the guard scans.

What it checks
──────────────
For every ``.github/workflows/*.yml`` that runs on ``pull_request``:

1. Find each ``scripts/check-*.{sh,py}`` invocation inside a job.
2. Work out which changed paths make that job (or step) run:

   * a job/step ``if:`` that reads ``needs.detect-changes.outputs.<name>``
     runs only when the ``<name>`` paths-filter in the ``detect-changes``
     job matches (several names joined by ``||`` are a union;
     ``any-testable`` is scraper | nlp | api | web);
   * a workflow-level ``on.pull_request.paths`` list gates every job;
   * anything else runs on every PR (unfiltered).

3. If a guard has at least one unfiltered invocation, it is fine.
4. Otherwise the guard must declare what it scans near the top of the
   invoked file, one or more lines of the form::

       # scans: packages/api/src/ scripts/dispatcher/daemon.py

   A path ending in ``/`` means everything below it; ``*`` and ``**`` are
   allowed.  Every declared path must be covered by the filter of at least
   one of the guard's invocations (a filter glob such as
   ``packages/api/**`` covers ``packages/api/src/``; ``packages/scraper-framework/**``
   does not cover ``packages/*/src/``).

An undeclared guard in a filtered job fails: the check cannot tell what it
scans, and a guard that scans outside ``scripts/`` is the case that slipped.

Usage:
    python3 scripts/check_guard_path_filters.py [--repo-root PATH]

Exit codes:
    0 — every guard runs on the PRs that touch what it scans.
    1 — one or more guards can be skipped by a PR they exist to catch
        (stderr carries a Fix: block).
    2 — usage error or a workflow this check cannot read.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The detect-changes parser lives in check-ci-job-skipped.py (hyphenated, so
# it cannot be imported by name).  Reuse it rather than keep a second copy.
_CI_JOB_SKIPPED = Path(__file__).resolve().parent / "check-ci-job-skipped.py"
if not _CI_JOB_SKIPPED.is_file():  # pragma: no cover - repo layout guard
    raise SystemExit(f"error: cannot find {_CI_JOB_SKIPPED}")
_spec = importlib.util.spec_from_file_location("_check_ci_job_skipped", _CI_JOB_SKIPPED)
assert _spec is not None and _spec.loader is not None
_cjs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _cjs  # dataclasses look the module up by name
_spec.loader.exec_module(_cjs)
parse_ci_yaml = _cjs.parse_ci_yaml
IF_OUTPUT_RE = _cjs.IF_OUTPUT_RE

GUARD_REF_RE = re.compile(r"scripts/(check-[A-Za-z0-9_.\-]+?\.(?:sh|py))\b")
SCANS_RE = re.compile(r"^#\s*scans:\s*(.+?)\s*$")
STEP_START_RE = re.compile(r"^(\s+)-\s")
STEP_IF_RE = re.compile(r"^\s+if:\s*(.+)$")
SCAN_HEADER_LINES = 80

# Computed detect-changes outputs that are not paths-filters themselves.
DERIVED_OUTPUTS: dict[str, tuple[str, ...]] = {
    "any-testable": ("scraper", "nlp", "api", "web"),
}

REPO_GUARDS_JOB = "repo-guards"


# ---------------------------------------------------------------------------
# Glob subsumption
# ---------------------------------------------------------------------------


def _seg_covers(g: str, r: str) -> bool:
    """True if filter segment ``g`` matches every name segment ``r`` can be."""
    if g == r:
        return True
    if r == "**":
        return False
    if g == "*":
        return True
    if any(c in r for c in "*?[") or not any(c in g for c in "*?["):
        return False
    return re.fullmatch(_seg_regex(g), r) is not None


def _seg_regex(seg: str) -> str:
    out = []
    for c in seg:
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
    return "".join(out)


def glob_covers(filter_glob: str, scan_path: str) -> bool:
    """True if every file matched by ``scan_path`` is matched by ``filter_glob``.

    ``scan_path`` ending in ``/`` stands for everything below that directory.
    Conservative: returns False when it cannot prove coverage.
    """
    g = filter_glob.strip().strip("'\"").split("/")
    sp = scan_path.strip()
    if sp.endswith("/"):
        sp += "**"
    r = sp.split("/")

    def rec(i: int, j: int) -> bool:
        if i == len(g):
            return j == len(r)
        if g[i] == "**":
            # ** matches zero or more of r's segments, including r's own **.
            return any(rec(i + 1, k) for k in range(j, len(r) + 1))
        if j == len(r):
            return False
        return _seg_covers(g[i], r[j]) and rec(i + 1, j + 1)

    return rec(0, 0)


def covered_by_any(globs: list[str], scan_path: str) -> bool:
    return any(glob_covers(g, scan_path) for g in globs if not g.startswith("!"))


# ---------------------------------------------------------------------------
# Workflow parsing
# ---------------------------------------------------------------------------


@dataclass
class PRTrigger:
    runs_on_pr: bool
    paths: list[str] | None  # None = no paths filter (every PR)


def parse_pr_trigger(lines: list[str]) -> PRTrigger:
    """Read ``on:`` for a pull_request trigger and its ``paths:`` list."""
    on_idx = None
    for i, ln in enumerate(lines):
        m = re.match(r"^[\"']?on[\"']?:\s*(.*?)\s*$", ln)
        if m:
            on_idx = i
            inline = m.group(1)
            if inline and not inline.startswith("#"):
                return PRTrigger("pull_request" in inline, None)
            break
    if on_idx is None:
        return PRTrigger(False, None)

    runs_on_pr = False
    paths: list[str] | None = None
    in_pr = False
    in_paths = False
    for ln in lines[on_idx + 1 :]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 0:
            break
        if indent == 2:
            key = ln.strip().split(":", 1)[0]
            in_pr = key in ("pull_request", "pull_request_target")
            runs_on_pr = runs_on_pr or in_pr
            in_paths = False
            continue
        if not in_pr:
            continue
        if indent == 4:
            key = ln.strip().split(":", 1)[0]
            in_paths = key == "paths"
            if in_paths and paths is None:
                paths = []
            continue
        if in_paths:
            m = re.match(r"^\s+-\s+(.+?)\s*$", ln)
            if m:
                item = m.group(1).split(" #", 1)[0].strip().strip("'\"")
                paths.append(item)  # type: ignore[union-attr]
    return PRTrigger(runs_on_pr, paths)


@dataclass
class Invocation:
    workflow: str
    job: str
    line: int  # 1-indexed
    gate: str  # human description of what runs the job
    globs: list[str] | None  # None = unfiltered
    pr_paths: list[str] | None


@dataclass
class Guard:
    name: str
    invocations: list[Invocation] = field(default_factory=list)


def _outputs_globs(names: list[str], filters: dict[str, list[str]]) -> list[str]:
    globs: list[str] = []
    for n in names:
        for sub in DERIVED_OUTPUTS.get(n, (n,)):
            globs.extend(filters.get(sub, []))
    return globs


def collect_invocations(
    workflow: Path, repo_root: Path
) -> list[tuple[str, Invocation]]:
    lines = workflow.read_text(encoding="utf-8").splitlines()
    trigger = parse_pr_trigger(lines)
    if not trigger.runs_on_pr:
        return []
    try:
        ci = parse_ci_yaml(workflow)
    except ValueError:
        return []
    rel = workflow.relative_to(repo_root).as_posix()
    found: list[tuple[str, Invocation]] = []
    for job in ci.jobs:
        if job.name == "detect-changes":
            continue
        job_names = job.if_gate_filter.split("|") if job.if_gate_filter else []
        step_names: list[str] = []
        step_indent: int | None = None
        for lineno in range(job.start_line + 1, job.end_line + 1):
            ln = lines[lineno - 1]
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m_step = STEP_START_RE.match(ln)
            if m_step and (step_indent is None or len(m_step.group(1)) <= step_indent):
                step_indent = len(m_step.group(1))
                step_names = []
            m_if = STEP_IF_RE.match(ln)
            if (
                m_if
                and step_indent is not None
                and len(ln) - len(ln.lstrip()) > step_indent
            ):
                step_names = IF_OUTPUT_RE.findall(m_if.group(1))
            for m in GUARD_REF_RE.finditer(ln):
                names = step_names or job_names
                globs = _outputs_globs(names, ci.filters) if names else None
                gate = (
                    "detect-changes." + " | ".join(names)
                    if names
                    else (
                        "on.pull_request.paths"
                        if trigger.paths is not None
                        else "every PR"
                    )
                )
                found.append(
                    (
                        m.group(1),
                        Invocation(rel, job.name, lineno, gate, globs, trigger.paths),
                    )
                )
    return found


def read_scans(guard_file: Path) -> list[str] | None:
    """Return the guard's declared ``# scans:`` paths, or None if undeclared."""
    if not guard_file.is_file():
        return None
    scans: list[str] = []
    seen = False
    with guard_file.open(encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh):
            if i >= SCAN_HEADER_LINES:
                break
            m = SCANS_RE.match(ln.strip())
            if m:
                seen = True
                scans.extend(m.group(1).split())
    return scans if seen else None


def invocation_covers(inv: Invocation, scan_path: str) -> bool:
    if inv.globs is not None and not covered_by_any(inv.globs, scan_path):
        return False
    if inv.pr_paths is not None and not covered_by_any(inv.pr_paths, scan_path):
        return False
    return True


def is_unfiltered(inv: Invocation) -> bool:
    return inv.globs is None and inv.pr_paths is None


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


@dataclass
class Problem:
    guard: str
    invocations: list[Invocation]
    undeclared: bool
    uncovered: list[str]


def check(repo_root: Path) -> list[Problem]:
    wf_dir = repo_root / ".github" / "workflows"
    guards: dict[str, Guard] = {}
    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        for name, inv in collect_invocations(wf, repo_root):
            guards.setdefault(name, Guard(name)).invocations.append(inv)

    problems: list[Problem] = []
    for name in sorted(guards):
        g = guards[name]
        if any(is_unfiltered(inv) for inv in g.invocations):
            continue
        scans = read_scans(repo_root / "scripts" / name)
        if scans is None:
            problems.append(Problem(name, g.invocations, True, []))
            continue
        uncovered = [
            sp
            for sp in scans
            if not any(invocation_covers(inv, sp) for inv in g.invocations)
        ]
        if uncovered:
            problems.append(Problem(name, g.invocations, False, uncovered))
    return problems


def format_problems(problems: list[Problem]) -> str:
    out: list[str] = [
        "check-guard-path-filters: these guards run only in path-filtered jobs that",
        "a PR can skip while still changing what the guard scans (#5088):",
        "",
    ]
    for p in problems:
        out.append(f"  scripts/{p.guard}")
        for inv in p.invocations:
            out.append(
                f"    wired in {inv.workflow}:{inv.line} job {inv.job!r}, runs on: {inv.gate}"
            )
        if p.undeclared:
            out.append(
                "    no `# scans:` declaration, so the check cannot tell what it scans"
            )
        else:
            for sp in p.uncovered:
                out.append(
                    f"    declares `# scans: {sp}`, which no invocation's filter covers"
                )
        out.append("")
    names = ", ".join(p.guard for p in problems)
    out += [
        "Fix: pick one per guard (" + names + "):",
        "",
        f"  1. Move the step into the unfiltered `{REPO_GUARDS_JOB}` job in",
        "     .github/workflows/ci.yml (no `if:` on detect-changes outputs), e.g.:",
        "",
        "       - name: <what the guard checks>",
        "         run: scripts/<guard>",
        "",
        "  2. If the guard only scans paths the job's filter already triggers on,",
        "     declare them near the top of the guard script:",
        "",
        "       # scans: packages/api/src/ scripts/dispatcher/daemon.py",
        "",
        "  3. Or widen the job's paths-filter (the detect-changes `filters:` entry",
        "     the `if:` reads, or the workflow's on.pull_request.paths) to cover",
        "     every declared path.",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[1] if __doc__ else None
    )
    ap.add_argument(
        "--repo-root",
        default=None,
        help="Repo root (default: this script's parent dir).",
    )
    args = ap.parse_args(argv)
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )
    if not (repo_root / ".github" / "workflows").is_dir():
        print(f"error: no .github/workflows under {repo_root}", file=sys.stderr)
        return 2
    problems = check(repo_root)
    if not problems:
        print(
            "check-guard-path-filters: every guard runs on the PRs that touch what it scans."
        )
        return 0
    print(format_problems(problems), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
