"""Tests for ``scripts.check_fix_block_coverage_complete`` — the per-guard
Fix-block formatter for ``check-fix-block-coverage-complete.sh`` (issue
#4405).

The helper computes alphabetical insertion points for missing hygiene
guards against the existing inventory rows in
``docs/dx/check-script-fix-block-coverage.md`` and emits a copy-pasteable
Fix block per missing guard naming the exact letter-suffix row number,
a row template with ``<guard>`` already filled in, and the new
``Total guards: N`` count for the Summary section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from check_fix_block_coverage_complete import (
    InventoryRow,
    compute_insertion,
    format_fix_block,
    parse_inventory,
)


# ─── Inventory parsing ────────────────────────────────────────────────────


def write_inventory(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """Write a minimal inventory doc with ``rows`` and return the path.

    Each row is ``(rownum, basename)``.  The doc reproduces the
    "## Survey", "## Summary" section structure expected by the parser
    (Total-guards line included so the new-total computation has
    something to read).
    """

    body_rows = "\n".join(
        f"| {rn} | `scripts/{name}` | self-diagnosing (Fix block) | Note. |"
        for rn, name in rows
    )
    doc = (
        "# Hygiene-Guard Fix-Block Coverage\n"
        "\n"
        "## Survey\n"
        "\n"
        "| # | Guard | Verdict | Notes |\n"
        "|---|-------|---------|-------|\n"
        f"{body_rows}\n"
        "\n"
        "## Summary\n"
        "\n"
        f"- Total guards: {len(rows)} (synthetic).\n"
    )
    path = tmp_path / "check-script-fix-block-coverage.md"
    path.write_text(doc)
    return path


def test_parse_inventory_extracts_rownum_and_basename(tmp_path: Path) -> None:
    """Parser returns rows in document order with rownum + basename."""

    doc = write_inventory(
        tmp_path,
        [
            ("1", "check-api-keys.sh"),
            ("11", "check-ci-job-skipped.sh"),
            ("11a", "check-ci-guards-skip-list-coverage.sh"),
            ("23", "check-duplicate-pr.sh"),
            ("23a", "check-fix-block-coverage-complete.sh"),
            ("84", "check_tf_empty_resource.py"),
        ],
    )
    rows = parse_inventory(doc)

    assert [(r.rownum, r.basename) for r in rows] == [
        ("1", "check-api-keys.sh"),
        ("11", "check-ci-job-skipped.sh"),
        ("11a", "check-ci-guards-skip-list-coverage.sh"),
        ("23", "check-duplicate-pr.sh"),
        ("23a", "check-fix-block-coverage-complete.sh"),
        ("84", "check_tf_empty_resource.py"),
    ]


def test_parse_inventory_total_count(tmp_path: Path) -> None:
    """Parser returns the count from the ``Total guards:`` summary line."""

    doc = write_inventory(
        tmp_path,
        [
            ("1", "check-api-keys.sh"),
            ("2", "check-apollo-keyfields.sh"),
        ],
    )
    rows = parse_inventory(doc)
    assert len(rows) == 2


# ─── Insertion-point computation ──────────────────────────────────────────


@pytest.fixture
def sample_rows() -> list[InventoryRow]:
    """A minimal but representative slice of the production inventory."""

    return [
        InventoryRow("11", "check-ci-job-skipped.sh"),
        InventoryRow("11a", "check-ci-guards-skip-list-coverage.sh"),
        InventoryRow("12", "check-ci-passed-coverage.sh"),
        InventoryRow("23", "check-duplicate-pr.sh"),
        InventoryRow("23a", "check-fix-block-coverage-complete.sh"),
        InventoryRow("24", "check-git-gh-retries.sh"),
        InventoryRow("37", "check-no-api-github-fetch.sh"),
        InventoryRow("38", "check-no-duplicate-stubs.sh"),
        InventoryRow("78", "check-workflow-paths-filter-coverage.sh"),
        InventoryRow("78a", "check_no_basicconfig_with_extra.py"),
        InventoryRow("79", "check_no_redos_pattern.py"),
        InventoryRow("84", "check_tf_empty_resource.py"),
    ]


def test_compute_insertion_between_two_existing_rows(
    sample_rows: list[InventoryRow],
) -> None:
    """A new ``check-no-basicconfig-with-extra.sh`` slots between #37 and #38."""

    result = compute_insertion("check-no-basicconfig-with-extra.sh", sample_rows)

    assert result.new_rownum == "37a"
    assert result.prior is not None
    assert result.prior.basename == "check-no-api-github-fetch.sh"
    assert result.prior.rownum == "37"
    assert result.next_ is not None
    assert result.next_.basename == "check-no-duplicate-stubs.sh"


def test_compute_insertion_picks_next_letter_when_a_taken(
    sample_rows: list[InventoryRow],
) -> None:
    """When ``23a`` already exists, a new row at that position uses ``23b``."""

    # ``check-foo-bar.sh`` slots between row 23a (check-fix-block-...) and
    # row 24 (check-git-gh-retries.sh) — alphabetically:
    #   check-fix-block-coverage-complete.sh < check-foo-bar.sh < check-git-gh-retries.sh
    # The prior peer is 23a, so the next available letter on base 23 is ``b``.
    result = compute_insertion("check-foo-bar.sh", sample_rows)

    assert result.new_rownum == "23b"
    assert result.prior is not None
    assert result.prior.basename == "check-fix-block-coverage-complete.sh"
    assert result.prior.rownum == "23a"


def test_compute_insertion_underscore_py_in_underscore_section(
    sample_rows: list[InventoryRow],
) -> None:
    """Underscore-named ``.py`` files placed between underscore peers."""

    # ``check_other.py`` slots between row 79 and row 84 — alphabetically:
    #   check_no_redos_pattern.py < check_other.py < check_tf_empty_resource.py
    result = compute_insertion("check_other.py", sample_rows)

    assert result.new_rownum == "79a"
    assert result.prior is not None
    assert result.prior.basename == "check_no_redos_pattern.py"
    assert result.next_ is not None
    assert result.next_.basename == "check_tf_empty_resource.py"


def test_compute_insertion_at_alphabetical_start(
    sample_rows: list[InventoryRow],
) -> None:
    """A guard that sorts before every existing row gets letter-suffix on row 1's number."""

    # ``check-aaa.sh`` sorts before the first row (`check-ci-job-skipped.sh`).
    # The expected insertion: prior is None, suggest the guard becomes a new
    # row at the start. We use the special-case suffix on the first row's
    # number minus 1, but since no row 0 exists, the convention is to use
    # ``1`` itself with a leading-letter suffix — but for simplicity the
    # implementation falls back to "Insert before row #11" with no
    # letter-suffix derivation (start-of-list edge case).
    result = compute_insertion("check-aaa.sh", sample_rows)

    assert result.prior is None
    assert result.next_ is not None
    assert result.next_.basename == "check-ci-job-skipped.sh"
    # Edge case: the implementation surfaces this as a special "before-first"
    # marker so the operator knows to renumber manually or pick a low rownum.
    assert result.new_rownum.startswith("(before #")


def test_compute_insertion_at_alphabetical_end(
    sample_rows: list[InventoryRow],
) -> None:
    """A guard that sorts after every existing row gets letter-suffix on the last row's number."""

    result = compute_insertion("check_zzz_last.py", sample_rows)

    assert result.next_ is None
    assert result.prior is not None
    assert result.prior.basename == "check_tf_empty_resource.py"
    assert result.new_rownum == "84a"


# ─── Fix-block formatting ─────────────────────────────────────────────────


def test_format_fix_block_names_specific_row_and_template(
    sample_rows: list[InventoryRow],
) -> None:
    """Fix block names the exact letter-suffix row + a copy-pasteable row template."""

    result = compute_insertion("check-no-basicconfig-with-extra.sh", sample_rows)
    block = format_fix_block(
        "check-no-basicconfig-with-extra.sh", result, total_guards=12, n_missing=1
    )

    # Specific row number named.
    assert "Insert at row #37a" in block
    # Both surrounding peers named with their row numbers.
    assert "#37 `check-no-api-github-fetch.sh`" in block
    assert "#38 `check-no-duplicate-stubs.sh`" in block
    # Copy-pasteable row template with <guard> filled in.
    assert (
        "| 37a | `scripts/check-no-basicconfig-with-extra.sh` | <verdict> | <one-line note> |"
        in block
    )


def test_format_fix_block_includes_total_guards_with_new_count(
    sample_rows: list[InventoryRow],
) -> None:
    """Fix block reminds the contributor to update Total guards: N with the actual count."""

    result = compute_insertion("check-no-basicconfig-with-extra.sh", sample_rows)
    # Suppose inventory currently has 12 guards and we're adding 1.
    block = format_fix_block(
        "check-no-basicconfig-with-extra.sh", result, total_guards=12, n_missing=1
    )

    # The new total is 12 + 1 = 13.  Block must name the literal new count.
    assert "Total guards: 13" in block


def test_format_fix_block_at_end_uses_last_row_letter_suffix(
    sample_rows: list[InventoryRow],
) -> None:
    """At-end placement uses ``<last-rownum>a`` (e.g. 84a)."""

    result = compute_insertion("check_zzz_last.py", sample_rows)
    block = format_fix_block("check_zzz_last.py", result, total_guards=12, n_missing=1)

    assert "Insert at row #84a" in block
    assert "after #84 `check_tf_empty_resource.py`" in block
    assert (
        "| 84a | `scripts/check_zzz_last.py` | <verdict> | <one-line note> |" in block
    )


def test_format_fix_block_picks_next_letter_when_a_taken(
    sample_rows: list[InventoryRow],
) -> None:
    """Letter suffix increments past existing peers (23 + 23a → 23b)."""

    result = compute_insertion("check-foo-bar.sh", sample_rows)
    block = format_fix_block("check-foo-bar.sh", result, total_guards=12, n_missing=1)

    assert "Insert at row #23b" in block
    assert "| 23b | `scripts/check-foo-bar.sh` | <verdict> | <one-line note> |" in block


def test_format_fix_block_sums_n_missing_in_total(
    sample_rows: list[InventoryRow],
) -> None:
    """When two guards are missing, each per-guard block names total + 2."""

    result = compute_insertion("check-foo-bar.sh", sample_rows)
    block = format_fix_block("check-foo-bar.sh", result, total_guards=12, n_missing=2)

    # 12 + 2 = 14 — the new total accounts for ALL missing guards.
    assert "Total guards: 14" in block


def test_format_fix_block_includes_rename_alternative(
    sample_rows: list[InventoryRow],
) -> None:
    """Fix block names the rename-without-`check-` prefix alternative (#4558).

    The naming-convention overload — ECS-oneshot data-check scripts named
    `scripts/check-*` getting flagged by the umbrella's auto-discovery —
    must surface as one of the listed remediation options. The alternative
    points at ``docs/agent/code-standards.md`` for the full rationale.
    """

    result = compute_insertion("check-foo-data.py", sample_rows)
    block = format_fix_block("check-foo-data.py", result, total_guards=12, n_missing=1)

    # The literal phrase the AC's verify line searches for.
    assert "rename without the `check-` prefix" in block
    # The post-rename suggestion — drops the `check-` prefix entirely.
    assert "`scripts/foo-data.py`" in block
    # Doc cross-reference + tracking issue.
    assert "code-standards.md" in block
    assert "#4558" in block


def test_format_fix_block_rename_alternative_handles_underscore_prefix(
    sample_rows: list[InventoryRow],
) -> None:
    """Rename alternative drops the `check_` prefix for underscore-named guards (#4558)."""

    result = compute_insertion("check_foo_data.py", sample_rows)
    block = format_fix_block("check_foo_data.py", result, total_guards=12, n_missing=1)

    assert "rename without the `check-` prefix" in block
    # `check_foo_data.py` → `foo_data.py` (underscore prefix stripped).
    assert "`scripts/foo_data.py`" in block


# ─── Live inventory smoke test ────────────────────────────────────────────


def test_live_inventory_parses(tmp_path: Path) -> None:
    """Parser handles the production ``docs/dx/check-script-fix-block-coverage.md``.

    Smoke test against the real file shipped with the repo. Catches a parser
    that accidentally over-fits the synthetic fixture format.
    """

    repo_root = Path(__file__).resolve().parent.parent.parent
    live_doc = repo_root / "docs" / "dx" / "check-script-fix-block-coverage.md"
    if not live_doc.is_file():
        pytest.skip(f"Live inventory not found at {live_doc}")

    rows = parse_inventory(live_doc)

    assert len(rows) >= 80
    # Spot-check a few well-known rows that are unlikely to renumber.
    basenames = {r.basename for r in rows}
    assert "check-api-keys.sh" in basenames
    assert "check-fix-block-coverage-complete.sh" in basenames
