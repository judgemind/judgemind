#!/usr/bin/env bash
# permanent: true
# check-cleanup-step-continue-on-error.sh — Verify best-effort cleanup steps
# in `.github/workflows/*.yml` carry `continue-on-error: true`.
#
# Why this check exists
# ---------------------
# A "cleanup step" is a step on the workflow's primary-success path that
# calls into the GitHub API to do best-effort housekeeping (typically:
# auto-closing resolved alert issues).  When the primary check has already
# passed, a transient GitHub-API 5xx in the cleanup branch should NOT fail
# the entire workflow run.  PRs #4234 (smoke-test) and #4238 (the other
# six workflows) added `continue-on-error: true` to seven such steps after
# real flakes.  This check prevents the same gap from being reintroduced.
#
# What gets flagged
# -----------------
# A step in any `.github/workflows/*.yml` that meets ALL THREE conditions:
#
#   1. The step is on a primary-success branch — at least one of:
#        a. its `name:` matches `^Auto-close` (case-insensitive), OR
#        b. its `if:` references `healthy == 'true'`, OR
#        c. its `if:` references `success() && ... has_failures == 'false'`
#
#   2. The step's `run:` block (or single-line `run:` value) invokes one
#      of `gh issue close`, `gh issue comment`, `gh issue list`, or
#      `gh api` — i.e. a cleanup that takes a GitHub-API side effect.
#
#   3. The step does NOT carry `continue-on-error: true` at the step
#      level.  (Job-level `continue-on-error` is intentionally NOT
#      considered — it would mask real failures in load-bearing steps.)
#
# Conservatism — what is NOT flagged
# ----------------------------------
# - Steps whose `if:` is `always()` or unset — a step that runs on the
#   failure path is load-bearing (it's the alert that opens the issue);
#   `continue-on-error: true` would mask real failures.  See #4238 where
#   `unblock-issues.yml`'s post-run audit is `if: always()` and is
#   intentionally left to a human operator's judgment, not this check.
# - Steps that don't call `gh issue close|comment|list` or `gh api` —
#   non-side-effecting cleanup is harmless.
# - The `issue-triage.yml` security gate — it strips `agent/ready` from
#   non-collaborator issues, which is load-bearing.  But that step's
#   `if:` is `if: github.event.action == 'opened' || ...`, so it does
#   not match any of the trigger conditions above.
#
# Usage
# -----
#   scripts/check-cleanup-step-continue-on-error.sh           # scan repo
#   scripts/check-cleanup-step-continue-on-error.sh DIR       # scan DIR/
#
# Exit codes
# ----------
#   0 — All clean: every cleanup-step that fires API calls carries
#       `continue-on-error: true`.
#   1 — One or more cleanup steps lack `continue-on-error: true`.
#       Each violation is printed as `path:line: <step-name>`.
#   2 — Script error (cannot find workflows directory, etc.).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

if [[ ! -d "$SCAN_DIR/.github/workflows" ]]; then
    # Tolerate scanning a subdir that has no workflows — exit 0 cleanly.
    if [[ "$SCAN_DIR" != "$REPO_ROOT" ]]; then
        exit 0
    fi
    echo "ERROR: $SCAN_DIR/.github/workflows directory not found." >&2
    exit 2
fi

# All YAML / structural parsing happens in the Python helper below.
# Keeping the structure-aware logic in Python is intentional: the
# step-block boundary detection ("until the next sibling step") is
# error-prone in pure awk/sed, and a Python implementation is easier
# to test (see scripts/tests/test_check_cleanup_step_continue_on_error.sh
# and tests/test_check_cleanup_step_continue_on_error.py).
exec python3 - "$SCAN_DIR/.github/workflows" <<'PYEOF'
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Trigger detection — the three "primary-success" patterns the AC names.
# ---------------------------------------------------------------------------
# (a) name: ^Auto-close...
NAME_AUTO_CLOSE_RE = re.compile(r"^Auto-close", re.IGNORECASE)
# (b) if: ... healthy == 'true'
IF_HEALTHY_RE = re.compile(r"healthy\s*==\s*'true'")
# (c) if: success() && ... has_failures == 'false'
#     The `&&` is allowed to be on a folded multi-line expression, so we
#     only verify the two atoms appear in the joined `if:` text.
IF_HAS_FAILURES_RE = re.compile(r"has_failures\s*==\s*'false'")
IF_SUCCESS_CALL_RE = re.compile(r"\bsuccess\s*\(\s*\)")

# ---------------------------------------------------------------------------
# Side-effect detection — the four `gh ...` commands the AC names.
# ---------------------------------------------------------------------------
# Patterns are anchored by leading word-boundary so `gh issue close` matches
# but `something_gh_issue_close` does not.
GH_SIDE_EFFECT_RE = re.compile(
    r"\bgh\s+(?:issue\s+(?:close|comment|list)|api)\b"
)


# ---------------------------------------------------------------------------
# Step parsing — a small line-level state machine over a single workflow file.
# ---------------------------------------------------------------------------
# A step header looks like:
#       - name: Foo                     <- starts a step
#       - uses: ./.github/actions/...   <- starts a step (no `name:`)
#       - run: foo                      <- starts a step (no `name:`)
#       - id: ...                       <- starts a step (rare, but valid)
# We treat any line of the form `^( *)- (name|uses|run|id|if|env|with|shell):`
# at indent N as a step header AT indent N. The step then continues
# until either (a) another step header appears at the same indent, or
# (b) we encounter a sibling job-level key (e.g. `jobs:`, a top-level key,
# or a job's `runs-on:`/`steps:`/`needs:` at a shallower indent).
#
# This is a heuristic; we deliberately under-collect rather than mis-collect.
# False negatives are tolerable (the next refactor will catch them); false
# positives on load-bearing steps are not.

STEP_HEADER_RE = re.compile(
    r"^(?P<indent> +)- (?P<key>name|uses|run|id|if|env|with|shell):\s*(?P<rest>.*)$"
)


class Step:
    """Accumulator for one step's parsed fields."""

    __slots__ = (
        "header_line",
        "indent",
        "name",
        "if_text",
        "continue_on_error",
        "run_text",
        "lines",
    )

    def __init__(self, header_line: int, indent: int) -> None:
        self.header_line = header_line  # 1-indexed line where the `- ` appears
        self.indent = indent
        self.name = ""
        self.if_text = ""
        self.continue_on_error = False
        self.run_text = ""
        self.lines: list[str] = []

    def trigger_matches(self) -> bool:
        if NAME_AUTO_CLOSE_RE.search(self.name.strip()):
            return True
        if IF_HEALTHY_RE.search(self.if_text):
            return True
        if IF_SUCCESS_CALL_RE.search(self.if_text) and IF_HAS_FAILURES_RE.search(
            self.if_text
        ):
            return True
        return False

    def has_gh_side_effect(self) -> bool:
        # Filter out comment lines — `# gh issue close` is documentation.
        # We strip leading-whitespace `#` lines from run_text first.
        non_comment = "\n".join(
            ln for ln in self.run_text.splitlines() if not ln.lstrip().startswith("#")
        )
        return bool(GH_SIDE_EFFECT_RE.search(non_comment))


def _parse_inline_value(rest: str) -> str:
    """Strip optional surrounding quotes from a YAML scalar value."""
    rest = rest.rstrip()
    if (rest.startswith("'") and rest.endswith("'")) or (
        rest.startswith('"') and rest.endswith('"')
    ):
        return rest[1:-1]
    return rest


def _read_block_scalar(
    lines: list[str], start_idx: int, marker_indent: int
) -> tuple[str, int]:
    """Read a `|`- or `>`-style block scalar starting at ``start_idx``.

    Returns ``(joined_text, next_idx)``. A block scalar continues as long
    as subsequent lines are either blank or indented strictly deeper than
    ``marker_indent`` (the indent of the `key: |` line).
    """
    out: list[str] = []
    j = start_idx
    n = len(lines)
    while j < n:
        ln = lines[j]
        if not ln.strip():
            out.append("")
            j += 1
            continue
        ln_indent = len(ln) - len(ln.lstrip(" "))
        if ln_indent <= marker_indent:
            break
        out.append(ln)
        j += 1
    return "\n".join(out), j


def parse_workflow(path: Path) -> list[Step]:
    """Parse one workflow file and return its list of Step objects."""
    text = path.read_text()
    lines = text.splitlines()

    steps: list[Step] = []
    i = 0
    n = len(lines)
    current: Step | None = None

    while i < n:
        line = lines[i]
        m = STEP_HEADER_RE.match(line)

        if m:
            indent = len(m.group("indent"))
            key = m.group("key")
            rest = m.group("rest")

            # Decide whether this header opens a NEW step or continues
            # an existing one. A new step opens when there is no current
            # step, OR when this `- ` line is at the same indent as the
            # current step's `- ` line (i.e. a sibling).
            if current is None or indent <= current.indent:
                if current is not None:
                    steps.append(current)
                current = Step(header_line=i + 1, indent=indent)

            current.lines.append(line)

            # Capture the field's value. The `- key:` line introduces the
            # *first* field of the step; subsequent fields are at the
            # step body indent (indent + 2).
            if key == "name":
                current.name = _parse_inline_value(rest)
            elif key == "if":
                if rest in ("|", ">", "|-", ">-", "|+", ">+", ">-\n"):
                    block, i = _read_block_scalar(lines, i + 1, indent + 2)
                    current.if_text = block.strip()
                    continue
                # Inline `if:` — but might be a folded multi-line `>-` style
                # or a multi-line YAML continuation (no `|` marker, lines
                # continue at deeper indent).  Capture trailing folded lines.
                if_val = _parse_inline_value(rest).rstrip()
                if if_val.endswith(">-") or if_val == ">-":
                    block, i = _read_block_scalar(lines, i + 1, indent + 2)
                    current.if_text = block.replace("\n", " ").strip()
                    continue
                # Look ahead for continuation lines at deeper indent
                # without an explicit block marker (rare, but the
                # smoke-test workflow uses `if: >-` block-scalar style).
                folded = [if_val]
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if not nxt.strip():
                        j += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                    if nxt_indent <= indent + 2:
                        break
                    # If the next line is itself another step body field
                    # (like `env:`, `run:`, `continue-on-error:`), stop.
                    body_field = re.match(
                        r"^\s+(name|if|env|run|with|continue-on-error|shell|id|uses|working-directory|timeout-minutes):",
                        nxt,
                    )
                    if body_field and nxt_indent == indent + 2:
                        break
                    folded.append(nxt.strip())
                    j += 1
                current.if_text = " ".join(folded).strip()
                i = j - 1  # outer loop will i += 1
            i += 1
            continue

        # Not a step header.  If we're inside a step, look for body fields.
        if current is not None:
            stripped = line.lstrip(" ")
            line_indent = len(line) - len(stripped)
            # Body fields live at current.indent + 2. Anything at
            # current.indent or shallower closes the step.
            if stripped and line_indent <= current.indent:
                # Close the current step.
                steps.append(current)
                current = None
                continue  # do not advance i; outer loop re-tests the line
            if line_indent == current.indent + 2:
                # Match `<key>: <rest>` for the body fields we care about.
                body_match = re.match(
                    r"^(?P<key>name|if|continue-on-error|run|env|with|shell|id|uses|working-directory|timeout-minutes):\s*(?P<rest>.*)$",
                    stripped,
                )
                if body_match:
                    bkey = body_match.group("key")
                    brest = body_match.group("rest")
                    current.lines.append(line)
                    if bkey == "name":
                        current.name = _parse_inline_value(brest)
                    elif bkey == "if":
                        if brest in ("|", ">", "|-", ">-", "|+", ">+"):
                            block, i = _read_block_scalar(
                                lines, i + 1, current.indent + 2
                            )
                            current.if_text = block.replace("\n", " ").strip()
                            continue
                        # Folded multi-line if (smoke-test style):
                        if brest.rstrip().endswith(">-") or brest.strip() == ">-":
                            block, i = _read_block_scalar(
                                lines, i + 1, current.indent + 2
                            )
                            current.if_text = block.replace("\n", " ").strip()
                            continue
                        if_val = _parse_inline_value(brest).rstrip()
                        folded = [if_val]
                        j = i + 1
                        while j < n:
                            nxt = lines[j]
                            if not nxt.strip():
                                j += 1
                                continue
                            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                            if nxt_indent <= current.indent + 2:
                                # Either sibling field or closes the step.
                                body_field = re.match(
                                    r"^\s+(name|env|run|with|continue-on-error|shell|id|uses|working-directory|timeout-minutes):",
                                    nxt,
                                )
                                if body_field and nxt_indent == current.indent + 2:
                                    break
                                # If at <= step-header indent, also stop.
                                if nxt_indent <= current.indent:
                                    break
                                # Comment line at body indent
                            folded.append(nxt.strip())
                            j += 1
                        current.if_text = " ".join(folded).strip()
                        i = j - 1
                    elif bkey == "continue-on-error":
                        val = _parse_inline_value(brest).strip().lower()
                        current.continue_on_error = val == "true"
                    elif bkey == "run":
                        if brest in ("|", ">", "|-", ">-", "|+", ">+"):
                            block, i = _read_block_scalar(
                                lines, i + 1, current.indent + 2
                            )
                            current.run_text = block
                            continue
                        # Inline `run: command` — capture as run_text.
                        current.run_text = _parse_inline_value(brest)
            elif line_indent > current.indent + 2:
                # Lines that belong to a deeper sub-mapping (env vars,
                # etc.) — append to step's lines for completeness but
                # don't reinterpret as body fields.
                current.lines.append(line)
            # else: blank line or comment line, fall through.

        i += 1

    if current is not None:
        steps.append(current)

    return steps


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "Usage: check-cleanup-step-continue-on-error.py <workflows-dir>",
            file=sys.stderr,
        )
        return 2
    workflows_dir = Path(argv[1])
    if not workflows_dir.is_dir():
        print(f"ERROR: not a directory: {workflows_dir}", file=sys.stderr)
        return 2

    violations: list[tuple[Path, int, str]] = []

    for f in sorted(workflows_dir.iterdir()):
        if not f.is_file():
            continue
        if f.suffix not in (".yml", ".yaml"):
            continue
        try:
            steps = parse_workflow(f)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR parsing {f}: {exc}", file=sys.stderr)
            return 2

        for step in steps:
            if not step.trigger_matches():
                continue
            if not step.has_gh_side_effect():
                continue
            if step.continue_on_error:
                continue
            display_name = step.name or "(unnamed step)"
            violations.append((f, step.header_line, display_name))

    if not violations:
        return 0

    print(
        "check-cleanup-step-continue-on-error: "
        "one or more best-effort cleanup steps lack `continue-on-error: true`.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path, line, name in violations:
        try:
            rel = path.relative_to(Path.cwd())
        except ValueError:
            rel = path
        print(f"  {rel}:{line}: {name}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Each step above is on a primary-success path (matching `^Auto-close`,",
        file=sys.stderr,
    )
    print(
        "`healthy == 'true'`, or `success() && ... has_failures == 'false'`) AND",
        file=sys.stderr,
    )
    print(
        "calls `gh issue close|comment|list` or `gh api` — meaning a transient",
        file=sys.stderr,
    )
    print(
        "GitHub-API 5xx will fail the entire workflow run even though the",
        file=sys.stderr,
    )
    print(
        "actual check has already passed.  Add `continue-on-error: true` at",
        file=sys.stderr,
    )
    print("the step level.  See #4234, #4238, #4241.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYEOF
