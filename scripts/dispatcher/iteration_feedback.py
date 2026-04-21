"""Helper: extract the iteration-feedback section from ``feedback.md``.

Callable both as a library module (imported by daemon.py) and as a
stand-alone script:

    python3 scripts/dispatcher/iteration_feedback.py {worktree}/tmp/ralph/feedback.md

When run as a script the function's return value is printed to stdout so
the calling skill can embed it in its terminal output.

Stdlib-only — no third-party dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Sentinel written by ``/task-v2-ralph`` Step 1 for the first iteration.
#: When feedback.md holds only this text (possibly with whitespace) there is
#: no real feedback to surface; the function returns an empty string.
_SEED_SENTINEL = "No prior feedback. This is the first iteration."


def build_iteration_feedback_section(feedback_md_path: str | Path) -> str:
    """Return a markdown section quoting ``feedback.md``, or ``""`` when empty.

    Returns a non-empty string only when the file:

    * exists, AND
    * contains something other than the seed sentinel
      (``"No prior feedback. This is the first iteration."``).

    The returned string has the form::

        ---
        ## Iteration feedback (from feedback.md)

        <verbatim content of feedback.md>

    This string is meant to be appended to the ``result`` field that
    ``/task-v2-ralph`` emits in its terminal JSON output so the daemon's
    ``_read_full_phase_log`` captures it into ``phase_outputs.log_text``.
    Downstream retry spawns can then query that column to surface prior
    failure context to fresh ralph workers.

    Parameters
    ----------
    feedback_md_path:
        Absolute or relative path to ``{worktree}/tmp/ralph/feedback.md``.

    Returns
    -------
    str
        Non-empty markdown section string, or ``""`` when there is nothing
        meaningful to surface (missing file or first-iteration sentinel).
    """
    path = Path(feedback_md_path)
    if not path.exists():
        return ""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    stripped = content.strip()
    if not stripped or stripped == _SEED_SENTINEL.strip():
        return ""

    return f"---\n## Iteration feedback (from feedback.md)\n\n{content}"


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) != 2:
        sys.stderr.write(f"Usage: {sys.argv[0]} <path-to-feedback.md>\n")
        sys.exit(1)
    result = build_iteration_feedback_section(sys.argv[1])
    if result:
        sys.stdout.write(result)
