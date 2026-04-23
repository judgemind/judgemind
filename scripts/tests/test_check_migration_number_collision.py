"""Tests for check-migration-number-collision.py — migration-number
collision detection across open PRs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# Import the module despite its dash-containing filename.
# Register it in sys.modules BEFORE exec_module so `@dataclass` can find
# its own module by name (it calls sys.modules[cls.__module__]).
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check-migration-number-collision.py"
)
_spec = importlib.util.spec_from_file_location(
    "check_migration_number_collision", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_migration_number_collision"] = mod
_spec.loader.exec_module(mod)

extract_migration_number = mod.extract_migration_number
parse_added_migration_numbers_from_diff = mod.parse_added_migration_numbers_from_diff
parse_gh_pr_list = mod.parse_gh_pr_list
find_collisions = mod.find_collisions
find_stale_numbers = mod.find_stale_numbers
OtherPR = mod.OtherPR


# ---------------------------------------------------------------------------
# extract_migration_number
# ---------------------------------------------------------------------------


class TestExtractMigrationNumber:
    def test_standard_path(self) -> None:
        assert (
            extract_migration_number(
                "packages/api/migrations/33_dispatcher-agents-failure-summary.sql"
            )
            == 33
        )

    def test_single_digit(self) -> None:
        assert (
            extract_migration_number("packages/api/migrations/1_initial-schema.sql")
            == 1
        )

    def test_two_digits(self) -> None:
        assert extract_migration_number("packages/api/migrations/42_whatever.sql") == 42

    def test_readme_ignored(self) -> None:
        assert extract_migration_number("packages/api/migrations/README.md") is None

    def test_other_dir_ignored(self) -> None:
        assert (
            extract_migration_number("packages/api/src/data-access/schema.sql") is None
        )

    def test_nested_subdir_ignored(self) -> None:
        # The regex anchors to direct children of migrations/.
        assert (
            extract_migration_number("packages/api/migrations/sub/1_nested.sql") is None
        )

    def test_no_number_prefix_ignored(self) -> None:
        assert (
            extract_migration_number("packages/api/migrations/initial-schema.sql")
            is None
        )

    def test_trailing_whitespace_stripped(self) -> None:
        assert (
            extract_migration_number(
                "packages/api/migrations/7_case-parties-unique-constraint.sql   "
            )
            == 7
        )


# ---------------------------------------------------------------------------
# parse_added_migration_numbers_from_diff
# ---------------------------------------------------------------------------


class TestParseAddedMigrationNumbersFromDiff:
    def test_single_added_migration(self) -> None:
        diff = (
            "diff --git a/packages/api/migrations/33_foo.sql "
            "b/packages/api/migrations/33_foo.sql\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/packages/api/migrations/33_foo.sql\n"
            "@@ -0,0 +1,1 @@\n"
            "+ALTER TABLE x ADD COLUMN y TEXT;\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == [33]

    def test_multiple_added_migrations(self) -> None:
        diff = (
            "diff --git a/packages/api/migrations/33_foo.sql "
            "b/packages/api/migrations/33_foo.sql\n"
            "new file mode 100644\n"
            "+++ b/packages/api/migrations/33_foo.sql\n"
            "diff --git a/packages/api/migrations/34_bar.sql "
            "b/packages/api/migrations/34_bar.sql\n"
            "new file mode 100644\n"
            "+++ b/packages/api/migrations/34_bar.sql\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == [33, 34]

    def test_ignores_non_migration_diffs(self) -> None:
        diff = (
            "diff --git a/scripts/foo.sh b/scripts/foo.sh\n"
            "+++ b/scripts/foo.sh\n"
            "diff --git a/packages/api/src/routes/index.ts "
            "b/packages/api/src/routes/index.ts\n"
            "+++ b/packages/api/src/routes/index.ts\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == []

    def test_empty_diff(self) -> None:
        assert parse_added_migration_numbers_from_diff("") == []

    def test_mixed_diff(self) -> None:
        diff = (
            "diff --git a/scripts/foo.sh b/scripts/foo.sh\n"
            "+++ b/scripts/foo.sh\n"
            "diff --git a/packages/api/migrations/5_bar.sql "
            "b/packages/api/migrations/5_bar.sql\n"
            "+++ b/packages/api/migrations/5_bar.sql\n"
            "diff --git a/README.md b/README.md\n"
            "+++ b/README.md\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == [5]

    def test_duplicate_numbers_deduplicated(self) -> None:
        # A rare scenario — e.g. a rename that creates two diff entries for
        # the same migration number. We still return a unique list.
        diff = (
            "diff --git a/packages/api/migrations/7_old.sql "
            "b/packages/api/migrations/7_old.sql\n"
            "+++ b/packages/api/migrations/7_old.sql\n"
            "diff --git a/packages/api/migrations/7_also-old.sql "
            "b/packages/api/migrations/7_also-old.sql\n"
            "+++ b/packages/api/migrations/7_also-old.sql\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == [7]

    def test_sorted_ascending(self) -> None:
        diff = (
            "diff --git a/packages/api/migrations/5_five.sql "
            "b/packages/api/migrations/5_five.sql\n"
            "+++ b/packages/api/migrations/5_five.sql\n"
            "diff --git a/packages/api/migrations/3_three.sql "
            "b/packages/api/migrations/3_three.sql\n"
            "+++ b/packages/api/migrations/3_three.sql\n"
        )
        assert parse_added_migration_numbers_from_diff(diff) == [3, 5]


# ---------------------------------------------------------------------------
# parse_gh_pr_list
# ---------------------------------------------------------------------------


class TestParseGhPrList:
    def test_empty_list(self) -> None:
        assert parse_gh_pr_list("[]") == []

    def test_single_pr_with_migration(self) -> None:
        payload = json.dumps(
            [
                {
                    "number": 2899,
                    "title": "admin table unify",
                    "files": [
                        {"path": "packages/api/migrations/33_foo.sql"},
                        {"path": "packages/api/src/routes/admin.ts"},
                    ],
                }
            ]
        )
        prs = parse_gh_pr_list(payload)
        assert len(prs) == 1
        assert prs[0].number == 2899
        assert prs[0].title == "admin table unify"
        assert prs[0].migration_numbers == [33]

    def test_pr_without_migration(self) -> None:
        payload = json.dumps(
            [
                {
                    "number": 2800,
                    "title": "docs: update readme",
                    "files": [{"path": "README.md"}],
                }
            ]
        )
        prs = parse_gh_pr_list(payload)
        assert len(prs) == 1
        assert prs[0].migration_numbers == []

    def test_multiple_prs_sorted_by_number(self) -> None:
        payload = json.dumps(
            [
                {
                    "number": 3000,
                    "title": "b",
                    "files": [{"path": "packages/api/migrations/34_b.sql"}],
                },
                {
                    "number": 2500,
                    "title": "a",
                    "files": [{"path": "packages/api/migrations/33_a.sql"}],
                },
            ]
        )
        prs = parse_gh_pr_list(payload)
        assert [p.number for p in prs] == [2500, 3000]

    def test_invalid_json_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            parse_gh_pr_list("not json")

    def test_non_array_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            parse_gh_pr_list('{"foo": "bar"}')

    def test_skips_malformed_entries(self) -> None:
        # Non-dict entries, missing-number entries — skipped silently.
        payload = json.dumps(
            [
                "not a dict",
                {"title": "no number", "files": []},
                {
                    "number": 42,
                    "title": "real one",
                    "files": [{"path": "packages/api/migrations/5_real.sql"}],
                },
            ]
        )
        prs = parse_gh_pr_list(payload)
        assert len(prs) == 1
        assert prs[0].number == 42
        assert prs[0].migration_numbers == [5]


# ---------------------------------------------------------------------------
# find_collisions — core logic
# ---------------------------------------------------------------------------


class TestFindCollisions:
    def test_no_migration_numbers_no_collisions(self) -> None:
        assert find_collisions([], [], current_pr_number=None) == []

    def test_no_other_prs_no_collisions(self) -> None:
        assert find_collisions([33], [], current_pr_number=1) == []

    def test_single_collision(self) -> None:
        other = OtherPR(number=2899, title="other", migration_numbers=[33])
        cols = find_collisions([33], [other], current_pr_number=2900)
        assert len(cols) == 1
        assert cols[0].my_number == 33
        assert cols[0].other_pr.number == 2899

    def test_self_excluded(self) -> None:
        # If the "other" PR IS me, no collision should be reported.
        me = OtherPR(number=2900, title="me", migration_numbers=[33])
        cols = find_collisions([33], [me], current_pr_number=2900)
        assert cols == []

    def test_no_collision_when_numbers_differ(self) -> None:
        other = OtherPR(number=2899, title="other", migration_numbers=[32])
        cols = find_collisions([33], [other], current_pr_number=2900)
        assert cols == []

    def test_multiple_other_prs_multiple_collisions(self) -> None:
        other_a = OtherPR(number=2899, title="a", migration_numbers=[33])
        other_b = OtherPR(number=2901, title="b", migration_numbers=[33, 34])
        cols = find_collisions([33, 34], [other_a, other_b], current_pr_number=2900)
        # Expect three collisions: (33, 2899), (33, 2901), (34, 2901)
        assert len(cols) == 3
        tuples = [(c.my_number, c.other_pr.number) for c in cols]
        assert tuples == [(33, 2899), (33, 2901), (34, 2901)]

    def test_collisions_sorted_deterministically(self) -> None:
        other_a = OtherPR(number=3000, title="a", migration_numbers=[50])
        other_b = OtherPR(number=2000, title="b", migration_numbers=[50])
        cols = find_collisions([50], [other_a, other_b], current_pr_number=1234)
        tuples = [(c.my_number, c.other_pr.number) for c in cols]
        assert tuples == [(50, 2000), (50, 3000)]

    def test_current_pr_number_none_excludes_nothing(self) -> None:
        # No current_pr_number → no self-exclusion. Every match counts.
        other = OtherPR(number=99, title="x", migration_numbers=[10])
        cols = find_collisions([10], [other], current_pr_number=None)
        assert len(cols) == 1


# ---------------------------------------------------------------------------
# find_stale_numbers — bonus stale-branch warning
# ---------------------------------------------------------------------------


class TestFindStaleNumbers:
    def test_none_latest_returns_empty(self) -> None:
        assert find_stale_numbers([33, 34], None) == []

    def test_no_my_numbers_returns_empty(self) -> None:
        assert find_stale_numbers([], 30) == []

    def test_all_above_latest_returns_empty(self) -> None:
        assert find_stale_numbers([34, 35], 33) == []

    def test_all_below_or_equal_latest(self) -> None:
        # latest_on_base = 33 means migration 33 is already applied.
        # Numbers <= 33 are stale.
        assert find_stale_numbers([33, 32], 33) == [32, 33]

    def test_mixed_some_stale_some_fine(self) -> None:
        assert find_stale_numbers([32, 33, 34], 33) == [32, 33]

    def test_result_sorted_ascending(self) -> None:
        assert find_stale_numbers([34, 30, 31], 33) == [30, 31]
