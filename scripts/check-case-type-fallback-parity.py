#!/usr/bin/env python3
"""Enforce case_type fallback chain parity between worker.py and reingest_from_s3.py.

Two code paths produce a final ``case_type`` for ruling documents:

* Live ingestion — ``packages/scraper-framework/src/ingestion/worker.py``
  applies ``extract_case_type_from_*`` helpers as post-LLM fallbacks just
  before the ``Field extraction summary`` log line.
* Reparse — ``scripts/reingest_from_s3.py`` calls the same helpers inside
  ``_apply_regex_fallbacks``.

The two paths must reference the **same set** of ``extract_case_type_from_*``
helpers.  When they diverge (a helper is added to one path but not the
other) the same document gets different case_type during live ingestion vs.
a reingest, which has caused multi-hour investigations and reingest re-runs
six times over four years (#1731, #1749, #1763, #1836, #2062 surfaced as
#4263, #2406).  This script is the cheap structural defense — see #4290.

Usage:
    scripts/check-case-type-fallback-parity.py

Exit codes:
    0 — Both files reference the same set of helpers.
    1 — Sets diverge (or a required file is missing).

Test override:
    Set ``CASE_TYPE_PARITY_ROOT`` to point at an alternate source tree.
    The script then reads
    ``$CASE_TYPE_PARITY_ROOT/packages/scraper-framework/src/ingestion/worker.py``
    and ``$CASE_TYPE_PARITY_ROOT/scripts/reingest_from_s3.py``.
"""

# permanent: true
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

WORKER_REL = "packages/scraper-framework/src/ingestion/worker.py"
REINGEST_REL = "scripts/reingest_from_s3.py"
REINGEST_FN = "_apply_regex_fallbacks"


def _repo_root() -> Path:
    override = os.environ.get("CASE_TYPE_PARITY_ROOT")
    if override:
        return Path(override).resolve()
    # This script lives at scripts/ in the repo root.
    return Path(__file__).resolve().parent.parent


def _scan_worker(text: str) -> set[str]:
    """Return the set of ``extract_case_type_from_*`` identifiers actually
    USED (referenced as expressions) in worker.py.

    The check intentionally scans the entire file rather than the post-LLM
    block alone — the reingest equivalent is a single helper function, but
    worker.py has multiple call sites for case_type fallbacks (the
    ``regex_fallback_ms`` timing block at ~line 2268 plus the post-LLM
    block at ~2451–2485) and a future refactor could legitimately move
    calls around.  The invariant we care about is "the set of helpers
    actually invoked is identical" — not "the helpers appear in a
    specific block of the file."

    Imports are not counted: ``from x import extract_case_type_from_title``
    appears as an ``ast.alias`` node, not ``ast.Name``, so a helper that
    is imported but never called will NOT appear in the returned set.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:  # pragma: no cover — parse failure surfaces immediately.
        print(
            f"ERROR: failed to parse worker.py as Python: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Collect identifiers actually USED (Name nodes in expression contexts —
    # i.e. function calls, attribute reads, etc.).  We do not require the
    # name to appear in a Call node specifically; an assignment like
    # ``fn = extract_case_type_from_X`` is also a usage.
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("extract_case_type_from_"):
            used.add(node.id)
    return used


def _scan_reingest(text: str) -> set[str]:
    """Return the set of ``extract_case_type_from_*`` identifiers called
    inside the ``_apply_regex_fallbacks`` function in
    ``scripts/reingest_from_s3.py``.

    We restrict to the function body — not the whole file — because the
    reingest module imports the helpers at module scope, and a stray
    reference outside ``_apply_regex_fallbacks`` (a comment, a docstring
    citation, an unrelated helper) would be a false positive.  The
    invariant the spec articulates is specifically "_apply_regex_fallbacks
    must call all worker.py helpers" — see issue #4290.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:  # pragma: no cover.
        print(
            f"ERROR: failed to parse reingest_from_s3.py as Python: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    target_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == REINGEST_FN:
            target_fn = node
            break

    if target_fn is None:
        print(
            f"ERROR: function ``{REINGEST_FN}`` not found in {REINGEST_REL}.\n"
            "       The parity guard relies on this function as the canonical\n"
            "       reingest fallback chain — if it has been renamed or\n"
            "       removed, update both check-case-type-fallback-parity.py\n"
            "       and the issue-#4290 documentation accordingly.",
            file=sys.stderr,
        )
        sys.exit(1)

    used: set[str] = set()
    for node in ast.walk(target_fn):
        if isinstance(node, ast.Name) and node.id.startswith("extract_case_type_from_"):
            used.add(node.id)
    return used


def _read(path: Path) -> str:
    if not path.is_file():
        print(
            f"ERROR: required source file is missing: {path}",
            file=sys.stderr,
        )
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def main() -> int:
    root = _repo_root()
    worker_path = root / WORKER_REL
    reingest_path = root / REINGEST_REL

    worker_text = _read(worker_path)
    reingest_text = _read(reingest_path)

    worker_set = _scan_worker(worker_text)
    reingest_set = _scan_reingest(reingest_text)

    if worker_set == reingest_set:
        joined = ", ".join(sorted(worker_set)) if worker_set else "(empty)"
        print(
            f"All clean — both paths reference the same case_type fallback "
            f"helpers: {joined}."
        )
        return 0

    only_worker = worker_set - reingest_set
    only_reingest = reingest_set - worker_set

    print(
        "ERROR: case_type fallback chain has diverged between worker.py and "
        "_apply_regex_fallbacks in reingest_from_s3.py.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    if only_worker:
        print(
            f"  Helpers called in {WORKER_REL}\n"
            f"  but missing from {REINGEST_FN} in {REINGEST_REL}:",
            file=sys.stderr,
        )
        for name in sorted(only_worker):
            print(f"    - {name}", file=sys.stderr)
        print("", file=sys.stderr)
    if only_reingest:
        print(
            f"  Helpers called in {REINGEST_FN} ({REINGEST_REL})\n"
            f"  but missing from {WORKER_REL}:",
            file=sys.stderr,
        )
        for name in sorted(only_reingest):
            print(f"    - {name}", file=sys.stderr)
        print("", file=sys.stderr)

    print(
        "  Add the missing call(s) so both paths produce the same case_type\n"
        "  for the same document.  See #4290 for context and the recurrence\n"
        "  history (#1731, #1749, #1763, #1836, #2062 -> #4263, #2406).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
