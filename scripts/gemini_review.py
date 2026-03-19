#!/usr/bin/env python3
# venv: scraper-framework
"""Gemini 2.5 Pro cross-model reviewer for the /ralph loop.

Reads task context and a git diff, sends them to Gemini for review,
and writes a verdict (SHIP or REVISE) plus detailed feedback.

Supports two modes:
  - standard (default): general code review
  - adversarial: bug-hunting focused review

Inputs (read from well-known paths):
  {worktree}/tmp/ralph/task.md          — acceptance criteria and issue context
  {worktree}/tmp/ralph/diff.txt         — git diff of all changes
  {worktree}/tmp/ralph/changed_files.txt — full content of changed files

Outputs (written to well-known paths):
  Standard mode:
    {worktree}/tmp/ralph/gemini-review-result.txt — "SHIP" or "REVISE"
    {worktree}/tmp/ralph/gemini-feedback.md       — detailed review feedback
  Adversarial mode:
    {worktree}/tmp/ralph/adversarial-result.txt   — "SHIP" or "REVISE"
    {worktree}/tmp/ralph/adversarial-feedback.md  — detailed adversarial findings

Environment variables:
  GOOGLE_API_KEY       — Required. Google API key for Gemini access.
  GEMINI_REVIEW_MODEL  — Optional. Model ID. Default: gemini-2.5-pro.
  GEMINI_REVIEW_MODE   — Optional. "standard" (default) or "adversarial".
  RALPH_STATE_DIR      — Required. Path to {worktree}/tmp/ralph/.

Exit codes:
  0 — Review completed successfully.
  1 — Error (missing inputs, API failure, etc.).
  2 — Skipped (no API key available). Treated as graceful degradation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ralph_review_log import (
    ReviewTimer,
    compute_diff_stats,
    log_review,
)


def read_file(path: Path) -> str:
    """Read a file and return its contents, or empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _output_names(mode: str) -> tuple[str, str]:
    """Return (result_filename, feedback_filename) for the given review mode."""
    if mode == "adversarial":
        return "adversarial-result.txt", "adversarial-feedback.md"
    return "gemini-review-result.txt", "gemini-feedback.md"


def _model_log_name(mode: str, model: str) -> str:
    """Return the model identifier used in review-log.jsonl."""
    if mode == "adversarial":
        return f"{model}-adversarial"
    return model


def build_review_prompt(task_md: str, diff_text: str, changed_files: str) -> str:
    """Build the standard review prompt for Gemini."""
    return f"""You are a cross-model code reviewer in an iterative review loop. Your job is to
evaluate whether a code implementation is ready to ship or needs revision. You are providing
an independent perspective — a different AI model wrote this code.

## Task Requirements

{task_md}

## Git Diff (all changes)

```diff
{diff_text}
```

## Full Content of Changed Files

{changed_files}

## Review Criteria

Evaluate the implementation against these criteria:

1. **Correctness**: Does the implementation satisfy the acceptance criteria listed in the task?
2. **Test coverage**: Are there tests for each acceptance criterion and obvious edge cases?
3. **Scope**: Are there changes unrelated to the task (scope creep, extra refactors, unrelated fixes)?
4. **Code quality**: Does it follow existing patterns? Any debug code, hardcoded values, or forgotten TODOs?
5. **Missing pieces**: Are there files that should have been created or modified but weren't?
6. **Stale references**: Do comments, imports, or docstrings reference things that changed?
7. **Documentation consistency**: If the change modifies behavior, configuration, or interfaces — do related docs (`docs/`, `CLAUDE.md`, `.claude/skills/`, `README.md`, `CONTRIBUTING.md`) need corresponding updates? Flag any docs that reference the old behavior.
8. **Performance**: Are there obvious bottlenecks? Sequential I/O that could be parallelized?
   O(n^2) patterns? Missing connection pooling or batching for network calls?

## Your Response

Start with a single word on the first line: either SHIP or REVISE.

Then provide your detailed review:
- If SHIP: briefly explain why the implementation meets the criteria.
- If REVISE: list specific, actionable feedback. Reference exact files, functions,
  and line numbers. Describe what needs to change and why. Be concrete — the implementer
  must be able to act on your feedback without guessing.

Be rigorous but not pedantic. Don't request style changes that don't affect correctness
or readability. Don't request changes outside the scope of the task. If tests pass and
acceptance criteria are met, lean toward SHIP."""


def build_adversarial_prompt(task_md: str, diff_text: str, changed_files: str) -> str:
    """Build the adversarial (bug-hunting) review prompt for Gemini."""
    return f"""You are doing an adversarial code review. Your goal is to find real bugs, not style nits.

Look for:
- Dead code and unused parameters
- Logic errors (off-by-one, short-circuit, wrong branches)
- Missing error handling and untested exception branches
- Concurrency safety (shared resources, race conditions, temp files)
- Deprecated codepaths
- Cross-component assumptions that could break in partial/retry scenarios

## Task Requirements

{task_md}

## Git Diff (all changes)

```diff
{diff_text}
```

## Full Content of Changed Files

{changed_files}

## Your Response

Start with a single word on the first line: either SHIP or REVISE.

- If SHIP: briefly confirm you found no real bugs.
- If REVISE: list each bug you found with:
  - The exact file and line number
  - What the bug is
  - Why it matters (what could go wrong in production)
  - A concrete fix suggestion

Only flag real bugs and correctness issues. Do NOT flag:
- Style preferences or naming conventions
- Missing documentation
- Suggestions that are nice-to-have but not bugs
- Changes outside the scope of the task"""


def _read_iteration(state_dir: Path) -> int:
    """Read the current ralph iteration number from the state directory.

    The ralph loop writes the iteration number to iteration.txt.
    Returns 0 if the file is missing or unreadable.
    """
    try:
        return int((state_dir / "iteration.txt").read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _resolve_worktree(state_dir: Path) -> Path | None:
    """Resolve the worktree path from the state directory.

    The state dir is {worktree}/tmp/ralph/, so the worktree is two levels up.
    """
    worktree = state_dir.parent.parent
    if (worktree / ".git").exists():
        return worktree
    return None


def run_review(state_dir: Path, *, mode: str = "standard") -> int:
    """Run the Gemini review and write results.

    Args:
        state_dir: Path to the ralph state directory.
        mode: Review mode — "standard" or "adversarial".

    Returns:
        0 on success, 1 on error, 2 on graceful skip.
    """
    if mode not in ("standard", "adversarial"):
        print(f"ERROR: Invalid review mode: {mode!r}. Use 'standard' or 'adversarial'.", file=sys.stderr)
        return 1

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    iteration = _read_iteration(state_dir)
    result_name, feedback_name = _output_names(mode)
    model = os.environ.get("GEMINI_REVIEW_MODEL", "gemini-2.5-pro")
    log_model = _model_log_name(mode, model)
    mode_label = "adversarial" if mode == "adversarial" else "standard"

    if not api_key:
        print(
            f"WARNING: GOOGLE_API_KEY not set. Skipping Gemini {mode_label} review (graceful degradation).",
            file=sys.stderr,
        )
        result_path = state_dir / result_name
        feedback_path = state_dir / feedback_name
        result_path.write_text("SKIPPED\n", encoding="utf-8")
        feedback_path.write_text(
            f"Gemini {mode_label} review skipped: GOOGLE_API_KEY not available.\n",
            encoding="utf-8",
        )
        # Log the skip
        worktree = _resolve_worktree(state_dir)
        diff_stats = compute_diff_stats(worktree) if worktree else None
        log_review(
            state_dir,
            iteration=iteration,
            model=log_model,
            verdict="SKIPPED",
            feedback="GOOGLE_API_KEY not available.",
            diff_stats=diff_stats,
        )
        return 2

    # Read inputs
    task_md = read_file(state_dir / "task.md")
    diff_text = read_file(state_dir / "diff.txt")
    changed_files = read_file(state_dir / "changed_files.txt")

    if not task_md:
        print("ERROR: task.md not found or empty in state directory.", file=sys.stderr)
        return 1
    if not diff_text:
        print("ERROR: diff.txt not found or empty in state directory.", file=sys.stderr)
        return 1

    if mode == "adversarial":
        prompt = build_adversarial_prompt(task_md, diff_text, changed_files)
    else:
        prompt = build_review_prompt(task_md, diff_text, changed_files)

    try:
        from google import genai  # type: ignore[import-untyped]
    except ImportError:
        print(
            "ERROR: google-genai package not installed. "
            "Install with: pip install google-genai",
            file=sys.stderr,
        )
        return 1

    # Compute diff stats before the API call
    worktree = _resolve_worktree(state_dir)
    diff_stats = compute_diff_stats(worktree) if worktree else None

    timer = ReviewTimer()
    try:
        client = genai.Client(api_key=api_key)
        with timer:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )

        review_text = response.text.strip() if response.text else ""

        # Extract token usage from response metadata if available
        input_tokens = None
        output_tokens = None
        usage = getattr(response, "usage_metadata", None)
        if usage:
            input_tokens = getattr(usage, "prompt_token_count", None)
            output_tokens = getattr(usage, "candidates_token_count", None)

    except Exception as exc:
        print(f"ERROR: Gemini {mode_label} API call failed: {exc}", file=sys.stderr)
        # Graceful degradation on API errors — don't block the review loop
        result_path = state_dir / result_name
        feedback_path = state_dir / feedback_name
        result_path.write_text("SKIPPED\n", encoding="utf-8")
        feedback_path.write_text(
            f"Gemini {mode_label} review skipped due to API error: {exc}\n",
            encoding="utf-8",
        )
        # Log the API error as a skipped review
        log_review(
            state_dir,
            iteration=iteration,
            model=log_model,
            verdict="SKIPPED",
            feedback=f"API error: {exc}",
            latency_ms=timer.latency_ms,
            diff_stats=diff_stats,
        )
        return 2

    if not review_text:
        print(f"ERROR: Gemini {mode_label} returned empty response.", file=sys.stderr)
        return 1

    # Parse verdict from first line
    first_line = review_text.split("\n", 1)[0].strip().upper()
    if first_line.startswith("SHIP"):
        verdict = "SHIP"
    elif first_line.startswith("REVISE"):
        verdict = "REVISE"
    else:
        # If the model didn't follow the format, treat as REVISE to be safe
        print(
            f"WARNING: Gemini {mode_label} response did not start with SHIP or REVISE "
            f"(got: {first_line!r}). Treating as REVISE.",
            file=sys.stderr,
        )
        verdict = "REVISE"

    # Write outputs
    result_path = state_dir / result_name
    feedback_path = state_dir / feedback_name

    result_path.write_text(f"{verdict}\n", encoding="utf-8")
    feedback_path.write_text(
        f"# Gemini {mode_label.title()} Review ({model})\n\n{review_text}\n",
        encoding="utf-8",
    )

    # Log the review to the structured JSONL log
    log_review(
        state_dir,
        iteration=iteration,
        model=log_model,
        verdict=verdict,
        feedback=review_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=timer.latency_ms,
        diff_stats=diff_stats,
    )

    print(f"Gemini {mode_label} review complete: {verdict}")
    return 0


def main() -> None:
    """Entry point."""
    state_dir_env = os.environ.get("RALPH_STATE_DIR", "")
    if not state_dir_env:
        print("ERROR: RALPH_STATE_DIR environment variable not set.", file=sys.stderr)
        sys.exit(1)

    state_dir = Path(state_dir_env)
    if not state_dir.is_dir():
        print(f"ERROR: State directory does not exist: {state_dir}", file=sys.stderr)
        sys.exit(1)

    mode = os.environ.get("GEMINI_REVIEW_MODE", "standard")
    exit_code = run_review(state_dir, mode=mode)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
