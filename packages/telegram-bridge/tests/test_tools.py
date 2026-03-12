"""Tests for the read-only tool definitions and execution."""

from __future__ import annotations

from pathlib import Path

from telegram_bridge.tools import (
    MAX_OUTPUT_BYTES,
    MAX_READ_LINES,
    TOOL_DEFINITIONS,
    _truncate,
    execute_glob_files,
    execute_grep_files,
    execute_read_file,
    execute_shell_command,
    execute_tool,
    is_command_allowed,
    is_sql_read_only,
)

# ── Tool definitions ───────────────────────────────────────────────────


class TestToolDefinitions:
    def test_has_four_tools(self) -> None:
        assert len(TOOL_DEFINITIONS) == 4

    def test_tool_names(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert names == {"read_file", "grep_files", "glob_files", "run_shell_command"}

    def test_all_tools_have_input_schema(self) -> None:
        for tool in TOOL_DEFINITIONS:
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"
            assert "properties" in tool["input_schema"]

    def test_all_tools_have_description(self) -> None:
        for tool in TOOL_DEFINITIONS:
            assert "description" in tool
            assert len(tool["description"]) > 10


# ── is_command_allowed() ───────────────────────────────────────────────


class TestIsCommandAllowed:
    def test_gh_issue_list(self) -> None:
        assert is_command_allowed(["gh", "issue", "list", "--state", "open"])

    def test_gh_issue_view(self) -> None:
        assert is_command_allowed(["gh", "issue", "view", "42"])

    def test_gh_pr_list(self) -> None:
        assert is_command_allowed(["gh", "pr", "list"])

    def test_gh_pr_view(self) -> None:
        assert is_command_allowed(["gh", "pr", "view", "100", "--json", "title"])

    def test_git_log(self) -> None:
        assert is_command_allowed(["git", "log", "--oneline", "-10"])

    def test_git_diff(self) -> None:
        assert is_command_allowed(["git", "diff", "HEAD~1"])

    def test_git_show(self) -> None:
        assert is_command_allowed(["git", "show", "HEAD"])

    def test_git_status(self) -> None:
        assert is_command_allowed(["git", "status"])

    def test_git_branch(self) -> None:
        assert is_command_allowed(["git", "branch", "-a"])

    def test_dev_db_query(self) -> None:
        assert is_command_allowed(["scripts/dev-db-query.sh", "SELECT 1"])

    def test_blocked_rm(self) -> None:
        assert not is_command_allowed(["rm", "-rf", "/"])

    def test_blocked_git_push(self) -> None:
        assert not is_command_allowed(["git", "push", "origin", "main"])

    def test_blocked_git_commit(self) -> None:
        assert not is_command_allowed(["git", "commit", "-m", "test"])

    def test_blocked_python(self) -> None:
        assert not is_command_allowed(["python3", "-c", "import os"])

    def test_blocked_gh_issue_edit(self) -> None:
        assert not is_command_allowed(["gh", "issue", "edit", "42"])

    def test_blocked_gh_pr_merge(self) -> None:
        assert not is_command_allowed(["gh", "pr", "merge", "100"])

    def test_empty_args(self) -> None:
        assert not is_command_allowed([])

    def test_single_prefix_match(self) -> None:
        """Verify prefix matching works with longer commands."""
        assert is_command_allowed(
            ["gh", "issue", "list", "--repo", "judgemind/judgemind", "--limit", "5"]
        )


# ── is_sql_read_only() ─────────────────────────────────────────────────


class TestIsSqlReadOnly:
    """Tests for the SQL read-only validation function."""

    # ── Allowed (read-only) queries ───────────────────────────────────

    def test_simple_select(self) -> None:
        assert is_sql_read_only("SELECT COUNT(*) FROM rulings")

    def test_select_with_where(self) -> None:
        assert is_sql_read_only("SELECT id, case_number FROM rulings WHERE state = 'CA'")

    def test_explain(self) -> None:
        assert is_sql_read_only("EXPLAIN SELECT * FROM courts")

    def test_with_cte_select(self) -> None:
        assert is_sql_read_only(
            "WITH recent AS (SELECT * FROM rulings WHERE created_at > '2024-01-01') "
            "SELECT * FROM recent"
        )

    def test_show(self) -> None:
        assert is_sql_read_only("SHOW TABLES")

    # ── Blocked (destructive) queries ─────────────────────────────────

    def test_drop_table(self) -> None:
        assert not is_sql_read_only("DROP TABLE rulings")

    def test_delete_from(self) -> None:
        assert not is_sql_read_only("DELETE FROM rulings WHERE id = 1")

    def test_update(self) -> None:
        assert not is_sql_read_only("UPDATE rulings SET status = 'deleted'")

    def test_alter_table(self) -> None:
        assert not is_sql_read_only("ALTER TABLE rulings ADD COLUMN foo TEXT")

    def test_truncate(self) -> None:
        assert not is_sql_read_only("TRUNCATE TABLE rulings")

    def test_insert(self) -> None:
        assert not is_sql_read_only("INSERT INTO rulings (id) VALUES (1)")

    def test_create_table(self) -> None:
        assert not is_sql_read_only("CREATE TABLE evil (id INT)")

    def test_grant(self) -> None:
        assert not is_sql_read_only("GRANT ALL ON rulings TO public")

    def test_revoke(self) -> None:
        assert not is_sql_read_only("REVOKE SELECT ON rulings FROM public")

    def test_replace(self) -> None:
        assert not is_sql_read_only("REPLACE INTO rulings (id) VALUES (1)")

    # ── Case insensitivity ────────────────────────────────────────────

    def test_lowercase_drop(self) -> None:
        assert not is_sql_read_only("drop table rulings")

    def test_mixed_case_delete(self) -> None:
        assert not is_sql_read_only("Delete FROM rulings")

    def test_uppercase_select_allowed(self) -> None:
        assert is_sql_read_only("SELECT 1")

    def test_lowercase_select_allowed(self) -> None:
        assert is_sql_read_only("select 1")

    # ── Edge cases ────────────────────────────────────────────────────

    def test_keyword_in_string_literal_allowed(self) -> None:
        """Keywords inside single-quoted strings should NOT trigger blocking."""
        assert is_sql_read_only("SELECT * FROM rulings WHERE status = 'DELETE'")

    def test_keyword_in_string_literal_drop(self) -> None:
        assert is_sql_read_only("SELECT * FROM rulings WHERE action = 'DROP TABLE'")

    def test_keyword_as_column_substring_allowed(self) -> None:
        """'updated_at' contains 'update' but is not a standalone keyword."""
        assert is_sql_read_only("SELECT updated_at FROM rulings")

    def test_empty_string(self) -> None:
        assert is_sql_read_only("")

    def test_multiple_destructive_keywords(self) -> None:
        assert not is_sql_read_only("DROP TABLE rulings; DELETE FROM courts")


# ── _truncate() ────────────────────────────────────────────────────────


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self) -> None:
        text = "x" * (MAX_OUTPUT_BYTES + 100)
        result = _truncate(text)
        assert len(result.encode("utf-8")) <= MAX_OUTPUT_BYTES + 50  # allow suffix
        assert "truncated" in result.lower()

    def test_custom_max_bytes(self) -> None:
        result = _truncate("hello world", max_bytes=5)
        assert "truncated" in result.lower()


# ── execute_read_file() ───────────────────────────────────────────────


class TestExecuteReadFile:
    def test_read_existing_file(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        result = execute_read_file(repo_root=tmp_path, path="test.txt")

        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_read_with_offset(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        lines = [f"line{i}" for i in range(10)]
        test_file.write_text("\n".join(lines))

        result = execute_read_file(repo_root=tmp_path, path="test.txt", offset=5)

        assert "line5" in result
        assert "line0" not in result

    def test_read_with_limit(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        lines = [f"line{i}" for i in range(100)]
        test_file.write_text("\n".join(lines))

        result = execute_read_file(repo_root=tmp_path, path="test.txt", limit=3)

        assert "line0" in result
        assert "line2" in result
        # line3 should not be in the output since limit is 3.
        assert "line99" not in result

    def test_limit_capped_at_max(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        test_file.write_text("x\n" * 1000)

        result = execute_read_file(repo_root=tmp_path, path="test.txt", limit=9999)

        # Should still read at most MAX_READ_LINES.
        line_count = len(result.strip().splitlines()) - 1  # subtract header
        assert line_count <= MAX_READ_LINES

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = execute_read_file(repo_root=tmp_path, path="nope.txt")
        assert "error" in result.lower()

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        result = execute_read_file(repo_root=tmp_path, path="../../../etc/passwd")
        assert "error" in result.lower()
        assert "outside" in result.lower()

    def test_directory_not_file(self, tmp_path: Path) -> None:
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = execute_read_file(repo_root=tmp_path, path="subdir")
        assert "error" in result.lower()

    def test_line_numbers_in_output(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        test_file.write_text("alpha\nbeta\ngamma\n")

        result = execute_read_file(repo_root=tmp_path, path="test.txt")

        # Should have line numbers like "     1  alpha"
        assert "1" in result
        assert "alpha" in result


# ── execute_grep_files() ──────────────────────────────────────────────


class TestExecuteGrepFiles:
    def test_grep_finds_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("def hello():\n    pass\n")
        (tmp_path / "bar.py").write_text("def world():\n    pass\n")

        result = execute_grep_files(repo_root=tmp_path, pattern="hello")

        assert "hello" in result
        assert "foo.py" in result

    def test_grep_with_glob_filter(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("target\n")
        (tmp_path / "foo.txt").write_text("target\n")

        result = execute_grep_files(repo_root=tmp_path, pattern="target", glob="*.py")

        assert "foo.py" in result
        assert "foo.txt" not in result

    def test_grep_no_matches(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_text("hello\n")

        result = execute_grep_files(repo_root=tmp_path, pattern="zzzznotfound")

        assert "no matches" in result.lower()

    def test_grep_max_results(self, tmp_path: Path) -> None:
        lines = [f"match line {i}" for i in range(100)]
        (tmp_path / "data.txt").write_text("\n".join(lines))

        result = execute_grep_files(repo_root=tmp_path, pattern="match line", max_results=5)

        output_lines = [line for line in result.splitlines() if "match line" in line]
        assert len(output_lines) == 5


# ── execute_glob_files() ─────────────────────────────────────────────


class TestExecuteGlobFiles:
    def test_glob_finds_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="*.py")

        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_glob_nested(self, tmp_path: Path) -> None:
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="src/*.py")

        assert "src/main.py" in result

    def test_glob_no_matches(self, tmp_path: Path) -> None:
        result = execute_glob_files(repo_root=tmp_path, pattern="*.nope")

        assert "no files" in result.lower()

    def test_glob_excludes_git_directory(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        (git_dir / "abc123").write_text("")
        (tmp_path / "real.py").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="**/*")

        assert "real.py" in result
        assert ".git" not in result

    def test_glob_excludes_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("")
        (tmp_path / "app.js").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="**/*.js")

        assert "app.js" in result
        assert "node_modules" not in result

    def test_glob_excludes_venv(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "site.py").write_text("")
        (tmp_path / "main.py").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="**/*.py")

        assert "main.py" in result
        assert ".venv" not in result

    def test_glob_excludes_pycache(self, tmp_path: Path) -> None:
        cache = tmp_path / "src" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-312.pyc").write_text("")
        (tmp_path / "src" / "mod.py").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="**/*")

        assert "mod.py" in result
        assert "__pycache__" not in result

    def test_glob_recursive_pattern(self, tmp_path: Path) -> None:
        """Verify recursive globs work with the new Path.glob() approach."""
        sub = tmp_path / "a" / "b" / "c"
        sub.mkdir(parents=True)
        (sub / "deep.py").write_text("")

        result = execute_glob_files(repo_root=tmp_path, pattern="**/*.py")

        assert "deep.py" in result


# ── execute_shell_command() ──────────────────────────────────────────


class TestExecuteShellCommand:
    def test_allowed_git_status(self, tmp_path: Path) -> None:
        result = execute_shell_command(repo_root=tmp_path, command="git status")
        # Either succeeds or shows an error about not a git repo.
        # The point is it doesn't get blocked by the allowlist.
        assert "error: command" not in result.lower()

    def test_blocked_command(self, tmp_path: Path) -> None:
        result = execute_shell_command(repo_root=tmp_path, command="rm -rf /")
        assert "not in the allowlist" in result.lower()

    def test_blocked_git_push(self, tmp_path: Path) -> None:
        result = execute_shell_command(repo_root=tmp_path, command="git push origin main")
        assert "not in the allowlist" in result.lower()

    def test_blocked_arbitrary_python(self, tmp_path: Path) -> None:
        result = execute_shell_command(
            repo_root=tmp_path, command="python3 -c 'import os; os.system(\"rm -rf /\")'"
        )
        assert "not in the allowlist" in result.lower()

    def test_empty_command(self, tmp_path: Path) -> None:
        result = execute_shell_command(repo_root=tmp_path, command="")
        assert "error" in result.lower()

    def test_git_log_runs(self, tmp_path: Path) -> None:
        """git log in a non-repo directory should produce an error but not be blocked."""
        result = execute_shell_command(repo_root=tmp_path, command="git log --oneline -5")
        # Not blocked by allowlist — will error about not a git repo.
        assert "not in the allowlist" not in result.lower()

    def test_dev_db_query_select_not_blocked(self, tmp_path: Path) -> None:
        """A SELECT query should pass SQL validation (will fail at execution, not validation)."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh "SELECT COUNT(*) FROM rulings"',
        )
        # Should NOT be blocked by SQL validation.
        assert "destructive sql blocked" not in result.lower()

    def test_dev_db_query_drop_blocked(self, tmp_path: Path) -> None:
        """A DROP query should be blocked by SQL validation."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh "DROP TABLE rulings"',
        )
        assert "destructive sql blocked" in result.lower()

    def test_dev_db_query_delete_blocked(self, tmp_path: Path) -> None:
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh "DELETE FROM rulings WHERE id = 1"',
        )
        assert "destructive sql blocked" in result.lower()

    def test_dev_db_query_update_blocked(self, tmp_path: Path) -> None:
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh "UPDATE rulings SET x = 1"',
        )
        assert "destructive sql blocked" in result.lower()

    def test_dev_db_query_no_arg_blocked(self, tmp_path: Path) -> None:
        """dev-db-query.sh with no SQL argument should be rejected."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command="scripts/dev-db-query.sh",
        )
        assert "requires a sql query" in result.lower()

    def test_dev_db_query_rw_flag_blocked(self, tmp_path: Path) -> None:
        """The --rw flag must be blocked via Telegram for defense-in-depth."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh --rw "UPDATE rulings SET x = 1"',
        )
        assert "--rw flag is not allowed" in result.lower()

    def test_dev_db_query_rw_flag_blocked_any_position(self, tmp_path: Path) -> None:
        """The --rw flag must be blocked even when not in the first position."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command='scripts/dev-db-query.sh "SELECT 1" --rw',
        )
        assert "--rw flag is not allowed" in result.lower()

    def test_dev_db_query_keyword_in_literal_allowed(self, tmp_path: Path) -> None:
        """Keywords inside string literals should not trigger blocking."""
        result = execute_shell_command(
            repo_root=tmp_path,
            command="scripts/dev-db-query.sh \"SELECT * FROM rulings WHERE status = 'DELETE'\"",
        )
        assert "destructive sql blocked" not in result.lower()


# ── execute_tool() dispatch ──────────────────────────────────────────


class TestExecuteTool:
    def test_dispatch_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "test.txt").write_text("content\n")

        result = execute_tool(
            repo_root=tmp_path,
            tool_name="read_file",
            tool_input={"path": "test.txt"},
        )

        assert "content" in result

    def test_dispatch_grep_files(self, tmp_path: Path) -> None:
        (tmp_path / "test.py").write_text("findme\n")

        result = execute_tool(
            repo_root=tmp_path,
            tool_name="grep_files",
            tool_input={"pattern": "findme"},
        )

        assert "findme" in result

    def test_dispatch_glob_files(self, tmp_path: Path) -> None:
        (tmp_path / "test.py").write_text("")

        result = execute_tool(
            repo_root=tmp_path,
            tool_name="glob_files",
            tool_input={"pattern": "*.py"},
        )

        assert "test.py" in result

    def test_dispatch_shell_command(self, tmp_path: Path) -> None:
        result = execute_tool(
            repo_root=tmp_path,
            tool_name="run_shell_command",
            tool_input={"command": "git status"},
        )
        # Not blocked by allowlist.
        assert "not in the allowlist" not in result.lower()

    def test_dispatch_unknown_tool(self, tmp_path: Path) -> None:
        result = execute_tool(
            repo_root=tmp_path,
            tool_name="dangerous_tool",
            tool_input={},
        )
        assert "unknown tool" in result.lower()
