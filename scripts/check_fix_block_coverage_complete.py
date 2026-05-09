#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_fix_block_coverage_complete.py — Per-guard Fix-block formatter.

Driven by ``scripts/check-fix-block-coverage-complete.sh``.  When the
shell guard discovers that one or more executable hygiene guards under
``scripts/check-*.{sh,py}`` are missing from the inventory at
``docs/dx/check-script-fix-block-coverage.md``, the wrapper invokes this
module to emit a per-guard Fix block that names:

  - The exact letter-suffix row number for the alphabetical insertion
    point (e.g. ``Insert at row #37a, between #37 ... and #38 ...``).
  - A copy-pasteable row template with ``<guard>`` already filled in.
  - The new ``Total guards: N`` count for the Summary section.

Design — why a separate Python module
-------------------------------------

Computing the alphabetical insertion + letter-suffix increment + new
total in shell is awkward and bug-prone.  Pulling the logic into a small
Python module keeps the wrapper script's discovery + diff path simple
and fast (the common case is "everything is documented, exit 0"), and
makes the formatter unit-testable from
``scripts/tests/test_check_fix_block_coverage_complete.py``.

Issue #4405 / parent #4376.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The inventory uses LC_ALL=C byte sort.  ASCII byte values:
#   '-' (0x2D) < '_' (0x5F)
# So the existing rows fall into two natural lexicographic groups:
#   1. hyphen-named (`check-*.sh` and hyphen-named `.py` files)
#   2. underscore-named (`check_*.py`)
# A Python ``str`` comparison reproduces this byte-order convention
# directly — no ``locale`` setup needed.

# Row format inside the survey table:
#   | 23a | `scripts/check-fix-block-coverage-complete.sh` | <verdict> | ... |
_ROW_RE = re.compile(
    r"^\|\s*(?P<rn>[0-9]+[a-z]?)\s*\|\s*`scripts/(?P<name>check[-_a-zA-Z0-9_]+\.(?:sh|py))`"
)

# Total-guards summary line:
#   - Total guards: 95 (#31a `check-issue-verify-sql.py` ...
_TOTAL_RE = re.compile(r"^-?\s*Total guards:\s*(?P<n>[0-9]+)\b")


@dataclass(frozen=True)
class InventoryRow:
    """One row from the survey table.

    Attributes
    ----------
    rownum:
        Row number string as it appears in the table — may carry a
        single lowercase letter suffix (``"23"``, ``"23a"``, ``"50b"``).
    basename:
        Guard basename, e.g. ``"check-fix-block-coverage-complete.sh"``.
    """

    rownum: str
    basename: str

    @property
    def base_number(self) -> int:
        """Numeric prefix of the rownum (``23a`` → ``23``)."""

        m = re.match(r"^([0-9]+)", self.rownum)
        assert m is not None, (
            f"InventoryRow.rownum has no numeric prefix: {self.rownum!r}"
        )
        return int(m.group(1))

    @property
    def letter_suffix(self) -> str:
        """The letter suffix, or empty string (``23a`` → ``"a"``, ``23`` → ``""``)."""

        return self.rownum[len(str(self.base_number)) :]


@dataclass(frozen=True)
class InsertionResult:
    """Computed insertion point for a missing guard.

    Attributes
    ----------
    new_rownum:
        The row number to assign the new guard. For start-of-list inserts
        (no prior peer), this is a marker like ``"(before #11)"`` so the
        operator picks a number manually.
    prior:
        The alphabetically-prior peer row, or ``None`` if the new guard
        sorts before every existing row.
    next_:
        The alphabetically-next peer row, or ``None`` if the new guard
        sorts after every existing row.
    """

    new_rownum: str
    prior: InventoryRow | None
    next_: InventoryRow | None


def parse_inventory(doc_path: Path) -> list[InventoryRow]:
    """Read ``doc_path`` and return inventory rows in document order.

    Lines outside the survey table are ignored.  The parser is whitespace
    tolerant — leading/trailing spaces inside table cells are trimmed.
    """

    rows: list[InventoryRow] = []
    with doc_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            m = _ROW_RE.match(line)
            if m is None:
                continue
            rows.append(InventoryRow(rownum=m.group("rn"), basename=m.group("name")))
    return rows


def parse_total_guards(doc_path: Path) -> int | None:
    """Return the integer from the ``Total guards: N`` line, or ``None``."""

    with doc_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.lstrip()
            m = _TOTAL_RE.match(stripped)
            if m is not None:
                return int(m.group("n"))
    return None


def compute_insertion(new_basename: str, rows: list[InventoryRow]) -> InsertionResult:
    """Compute the alphabetical insertion point + letter-suffix for ``new_basename``.

    Algorithm:

    1. Find the alphabetical position by byte-comparing ``new_basename``
       against the existing ``rows`` (already in document order, which
       matches the LC_ALL=C byte-sort order the inventory enforces).
    2. The "prior peer" is the row immediately before the insertion
       position.  The "next peer" is the row at the insertion position.
    3. Letter suffix for the new row:
        - If no prior peer: special-case marker ``(before #<next>.rownum)``.
        - Else: take the prior's ``base_number``; find every existing row
          with that ``base_number`` (including the prior itself); pick
          the next available lowercase letter ('a', 'b', ...). When the
          prior has no letter suffix, the first available letter is
          ``a`` (so we get ``23 → 23a``).  When ``a`` is taken, ``b``,
          and so on.
    """

    # Walk in document order.  ``i`` is the first index whose basename
    # sorts AFTER ``new_basename`` — that is the insertion position.
    i = 0
    while i < len(rows) and rows[i].basename < new_basename:
        i += 1

    prior = rows[i - 1] if i > 0 else None
    next_ = rows[i] if i < len(rows) else None

    if prior is None:
        assert next_ is not None, "compute_insertion called against empty inventory"
        return InsertionResult(
            new_rownum=f"(before #{next_.rownum})", prior=prior, next_=next_
        )

    base = prior.base_number
    taken_letters = {
        r.letter_suffix for r in rows if r.base_number == base and r.letter_suffix
    }
    for code in range(ord("a"), ord("z") + 1):
        candidate = chr(code)
        if candidate not in taken_letters:
            return InsertionResult(
                new_rownum=f"{base}{candidate}", prior=prior, next_=next_
            )

    # Pathological — every letter a..z is taken at this base. Fall back
    # to a marker so the operator picks a non-conflicting rownum manually.
    return InsertionResult(
        new_rownum=f"(after #{prior.rownum})", prior=prior, next_=next_
    )


def format_fix_block(
    new_basename: str,
    result: InsertionResult,
    *,
    total_guards: int,
    n_missing: int,
) -> str:
    """Format the per-guard Fix block.

    ``n_missing`` is the total number of missing guards in this run; it
    drives the ``Total guards: <new-count>`` line so each per-guard block
    advertises the correct final count rather than current+1.
    """

    new_total = total_guards + n_missing
    rownum = result.new_rownum

    if result.prior is None and result.next_ is not None:
        # Start-of-list edge case.
        location_line = (
            f"Insert at row {rownum}, "
            f"before #{result.next_.rownum} `{result.next_.basename}`."
        )
    elif result.next_ is None and result.prior is not None:
        # End-of-list edge case.
        location_line = (
            f"Insert at row #{rownum}, "
            f"after #{result.prior.rownum} `{result.prior.basename}`."
        )
    elif result.prior is not None and result.next_ is not None:
        location_line = (
            f"Insert at row #{rownum}, "
            f"between #{result.prior.rownum} `{result.prior.basename}` "
            f"and #{result.next_.rownum} `{result.next_.basename}`."
        )
    else:  # pragma: no cover — empty-inventory path is rejected upstream
        location_line = f"Insert at row #{rownum} (inventory empty)."

    template_rownum = rownum.lstrip("(").rstrip(")")
    # The template strips the "(before/after ...)" wrapper for the literal
    # patch — operators paste this verbatim into the table.
    if template_rownum.startswith("before #") or template_rownum.startswith("after #"):
        # Pathological / start-of-list — use the prior/next rownum as a hint.
        if result.next_ is not None:
            template_rownum = f"{result.next_.base_number}_NEW"
        else:
            template_rownum = "1"

    template_line = f"| {template_rownum} | `scripts/{new_basename}` | <verdict> | <one-line note> |"

    # Compute the rename-without-`check-`-prefix suggestion for the
    # alternative remediation hint (#4558). The hint applies when the
    # missing guard is actually an ECS-oneshot data-check script (`# venv:`
    # + `# permanent: true` headers, invoked via scripts/ecs-run-task.sh
    # with a required argument like --date YYYY-MM-DD) rather than a true
    # code-quality CI guard. Rename drops the `check-`/`check_` prefix.
    if new_basename.startswith("check-"):
        renamed = new_basename[len("check-") :]
    elif new_basename.startswith("check_"):
        renamed = new_basename[len("check_") :]
    else:
        renamed = new_basename

    return (
        f"Fix for `scripts/{new_basename}`:\n"
        f"  {location_line}\n"
        f"  Row template (paste into the survey table):\n"
        f"    {template_line}\n"
        f"  Update the Summary section's count: Total guards: {new_total}\n"
        f"  Verdict options: self-diagnosing (Fix block), self-diagnosing (actionable text),\n"
        f"  wrapper (delegates to helper), operational health probe, decision flow, NEEDS UPGRADE.\n"
        f"  Alternative — rename without the `check-` prefix. If `{new_basename}` is\n"
        f"  an ECS-oneshot data-check script (`# venv:` + `# permanent: true` headers,\n"
        f"  invoked by scripts/ecs-run-task.sh with a required argument like\n"
        f"  --date YYYY-MM-DD) rather than a code-quality CI guard, rename to e.g.\n"
        f"  `scripts/{renamed}` so it falls outside the umbrella's auto-discovery\n"
        f"  namespace. See docs/agent/code-standards.md §\"Naming convention: don't\n"
        f'  name ECS-oneshot data-check scripts scripts/check-*.{{sh,py}}" (#4558).\n'
    )


# ─── CLI ────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit per-guard Fix blocks for missing hygiene guards. "
            "Driven by check-fix-block-coverage-complete.sh."
        )
    )
    parser.add_argument(
        "--doc",
        type=Path,
        required=True,
        help="Path to docs/dx/check-script-fix-block-coverage.md.",
    )
    parser.add_argument(
        "missing",
        nargs="+",
        help="One or more basenames of missing guards (e.g. check-foo.sh).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not args.doc.is_file():
        print(f"ERROR: inventory doc not found: {args.doc}", file=sys.stderr)
        return 2

    rows = parse_inventory(args.doc)
    if not rows:
        print(
            f"ERROR: no inventory rows parsed from {args.doc}",
            file=sys.stderr,
        )
        return 2

    total = parse_total_guards(args.doc)
    if total is None:
        # Fall back to the row count — the Summary section may be missing
        # in synthetic fixtures.  Not a fatal error.
        total = len(rows)

    n_missing = len(args.missing)
    for basename in args.missing:
        result = compute_insertion(basename, rows)
        block = format_fix_block(
            basename, result, total_guards=total, n_missing=n_missing
        )
        print(block, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
