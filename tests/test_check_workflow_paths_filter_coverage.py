#!/usr/bin/env python3
"""Tests for scripts/check-workflow-paths-filter-coverage.py (#4084).

Each test builds a synthetic .github/workflows/ + .github/actions/ tree
under tmp_path, runs the script, and asserts the exit code and output.

Fixture coverage (per issue #4084 acceptance criteria):
  1. in-paths-direct
       Workflow's run: block invokes scripts/foo.sh; foo.sh is in
       on.push.paths — exit 0.
  2. in-paths-via-composite-action
       Workflow uses ./.github/actions/myaction; the action's run:
       block invokes scripts/foo.sh; foo.sh is in on.push.paths
       — exit 0.
  3. missing-direct
       Workflow's run: block invokes scripts/foo.sh; foo.sh is NOT
       in on.push.paths — exit 1, error message names foo.sh.
  4. missing-via-composite
       Workflow uses a composite action that invokes scripts/foo.sh;
       foo.sh is NOT in on.push.paths — exit 1.
  5. in-Docker-container-invocation
       Workflow uses ecs-oneshot composite with
       command: '["node", "scripts/seed.mjs"]' under with: — that
       runs *inside* a Docker container, not on the runner, so the
       check should NOT flag scripts/seed.mjs even though it's not
       in on.push.paths. Exit 0.

Run from the repo root:
    pytest tests/test_check_workflow_paths_filter_coverage.py -v
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-workflow-paths-filter-coverage.py"


# ---------------------------------------------------------------------------
# Module loading (script has hyphens in its name → load via importlib)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module():
    spec = importlib.util.spec_from_file_location(
        "check_workflow_paths_filter_coverage", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_workflow_paths_filter_coverage"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create the .github tree and return (repo_root, workflows_dir, actions_dir)."""
    repo_root = tmp_path / "repo"
    workflows_dir = repo_root / ".github" / "workflows"
    actions_dir = repo_root / ".github" / "actions"
    workflows_dir.mkdir(parents=True)
    actions_dir.mkdir(parents=True)
    return repo_root, workflows_dir, actions_dir


def _run_script(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo_root),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# End-to-end fixture tests (the five cases listed in #4084 AC #3)
# ---------------------------------------------------------------------------


class TestFixtureCases:
    def test_in_paths_direct_passes(self, tmp_path: Path) -> None:
        """1. Script invoked directly + present in paths → exit 0."""
        repo_root, workflows_dir, _ = _make_repo(tmp_path)
        (workflows_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - 'scripts/foo.sh'\n"
            "jobs:\n"
            "  deploy:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: bash scripts/foo.sh\n"
        )
        result = _run_script(repo_root)
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}\nstderr={result.stderr!r}"
        )

    def test_in_paths_via_composite_action_passes(self, tmp_path: Path) -> None:
        """2. Script invoked via composite action + present in paths → exit 0."""
        repo_root, workflows_dir, actions_dir = _make_repo(tmp_path)
        (workflows_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - 'scripts/foo.sh'\n"
            "      - '.github/actions/myaction/**'\n"
            "jobs:\n"
            "  deploy:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: ./.github/actions/myaction\n"
        )
        action_dir = actions_dir / "myaction"
        action_dir.mkdir()
        (action_dir / "action.yml").write_text(
            "name: My Action\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      run: |\n"
            "        ${GITHUB_WORKSPACE}/scripts/foo.sh\n"
        )
        result = _run_script(repo_root)
        assert result.returncode == 0, (
            f"expected exit 0, got {result.returncode}\nstderr={result.stderr!r}"
        )

    def test_missing_direct_fails(self, tmp_path: Path) -> None:
        """3. Script invoked directly + missing from paths → exit 1, names script."""
        repo_root, workflows_dir, _ = _make_repo(tmp_path)
        (workflows_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "jobs:\n"
            "  deploy:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: bash scripts/foo.sh\n"
        )
        result = _run_script(repo_root)
        assert result.returncode == 1, (
            f"expected exit 1, got {result.returncode}\n"
            f"stdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )
        assert "scripts/foo.sh" in result.stderr
        assert "deploy.yml" in result.stderr

    def test_missing_via_composite_fails(self, tmp_path: Path) -> None:
        """4. Script invoked via composite + missing from paths → exit 1."""
        repo_root, workflows_dir, actions_dir = _make_repo(tmp_path)
        (workflows_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - '.github/actions/myaction/**'\n"
            "jobs:\n"
            "  deploy:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: ./.github/actions/myaction\n"
        )
        action_dir = actions_dir / "myaction"
        action_dir.mkdir()
        (action_dir / "action.yml").write_text(
            "name: My Action\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      run: |\n"
            "        ${GITHUB_WORKSPACE}/scripts/foo.sh\n"
        )
        result = _run_script(repo_root)
        assert result.returncode == 1, (
            f"expected exit 1, got {result.returncode}\nstderr={result.stderr!r}"
        )
        assert "scripts/foo.sh" in result.stderr
        # Violation should reference the composite action's location
        assert "myaction/action.yml" in result.stderr

    def test_in_docker_container_invocation_ignored(self, tmp_path: Path) -> None:
        """5. Script in `command:` input (runs inside container) → not flagged → exit 0.

        The ecs-oneshot composite action takes a ``command`` input and
        passes it as ``aws ecs register-task-definition --command``;
        the script runs *inside* the launched ECS container, not on
        the GitHub Actions runner. The check must not flag it even
        though the script is not in the paths filter.
        """
        repo_root, workflows_dir, actions_dir = _make_repo(tmp_path)
        (workflows_dir / "deploy.yml").write_text(
            "name: Deploy\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - '.github/actions/ecs-oneshot/**'\n"
            "jobs:\n"
            "  deploy:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: ./.github/actions/ecs-oneshot\n"
            "        with:\n"
            '          command: \'["node", "scripts/seed.mjs"]\'\n'
        )
        action_dir = actions_dir / "ecs-oneshot"
        action_dir.mkdir()
        # Action body does NOT itself invoke any scripts/ — `command:`
        # is forwarded to ECS register-task-definition, not executed
        # on the runner.
        (action_dir / "action.yml").write_text(
            "name: ECS Oneshot\n"
            "inputs:\n"
            "  command:\n"
            "    required: true\n"
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      env:\n"
            "        COMMAND: ${{ inputs.command }}\n"
            "      run: |\n"
            '        aws ecs register-task-definition --command "$COMMAND"\n'
        )
        result = _run_script(repo_root)
        assert result.returncode == 0, (
            f"expected exit 0 (in-container script must be ignored), "
            f"got {result.returncode}\n"
            f"stdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Unit-level tests on the parser/glob helpers
# ---------------------------------------------------------------------------


class TestGlobToRegex:
    def test_recursive_doublestar(self, module) -> None:
        pat = module.glob_to_regex("packages/**")
        assert pat.match("packages/a/b/c.py")
        assert pat.match("packages/")
        assert not pat.match("scripts/foo.sh")

    def test_exact_path(self, module) -> None:
        pat = module.glob_to_regex("scripts/foo.sh")
        assert pat.match("scripts/foo.sh")
        assert not pat.match("scripts/foo.shell")
        assert not pat.match("scripts/sub/foo.sh")

    def test_negation_handled_by_caller(self, module) -> None:
        # The glob_to_regex helper itself does NOT handle leading `!`;
        # WorkflowPaths separates positives from negatives. Documented
        # so a future refactor doesn't change the contract.
        pat = module.glob_to_regex("scripts/foo.sh")
        assert pat.match("scripts/foo.sh")


class TestFindScriptInvocations:
    def test_finds_bash_script(self, module) -> None:
        refs = module.find_script_invocations("bash scripts/foo.sh")
        assert refs == ["scripts/foo.sh"]

    def test_finds_python_script(self, module) -> None:
        refs = module.find_script_invocations("python3 scripts/check.py --flag")
        assert refs == ["scripts/check.py"]

    def test_finds_workspace_prefixed(self, module) -> None:
        refs = module.find_script_invocations('"${GITHUB_WORKSPACE}/scripts/wait.sh"')
        assert refs == ["scripts/wait.sh"]

    def test_dedupes_repeated_refs(self, module) -> None:
        refs = module.find_script_invocations("scripts/foo.sh\nscripts/foo.sh --flag\n")
        assert refs == ["scripts/foo.sh"]

    def test_skips_comment_lines(self, module) -> None:
        refs = module.find_script_invocations(
            "# scripts/foo.sh is a doc reference\necho hello\n"
        )
        assert refs == []

    def test_does_not_match_packages_scripts_subpath(self, module) -> None:
        # `packages/scripts/foo.sh` is not the same as `scripts/foo.sh`.
        refs = module.find_script_invocations("ls packages/scripts/foo.sh")
        assert "packages/scripts/foo.sh" not in refs
        # Nor should it match scripts/foo.sh by accident.
        assert refs == []

    def test_handles_multiple_per_line(self, module) -> None:
        refs = module.find_script_invocations("scripts/a.sh && scripts/b.sh")
        assert refs == ["scripts/a.sh", "scripts/b.sh"]


class TestPathsFilterParse:
    def test_extracts_positives_and_negatives(self, module, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - 'scripts/foo.sh'\n"
            "      - '!scripts/foo/**'\n"
            "jobs:\n"
            "  x:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo hi\n"
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=True)
        assert len(parsed.paths_filters) == 1
        pf = parsed.paths_filters[0]
        assert pf.trigger == "push"
        assert pf.positives == ["packages/**", "scripts/foo.sh"]
        assert pf.negatives == ["scripts/foo/**"]

    def test_extracts_both_push_and_pull_request(self, module, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "  pull_request:\n"
            "    branches: [main]\n"
            "    paths:\n"
            "      - 'packages/**'\n"
            "      - 'tests/**'\n"
            "jobs:\n"
            "  x:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: echo hi\n"
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=True)
        triggers = sorted(pf.trigger for pf in parsed.paths_filters)
        assert triggers == ["pull_request", "push"]

    def test_workflow_dispatch_only_yields_no_filters(
        self, module, tmp_path: Path
    ) -> None:
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "on:\n"
            "  workflow_dispatch:\n"
            "jobs:\n"
            "  x:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: bash scripts/foo.sh\n"
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=True)
        assert parsed.paths_filters == []


class TestRunBlockExtraction:
    def test_extracts_multiline_run_block(self, module, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "jobs:\n"
            "  x:\n"
            "    steps:\n"
            "      - run: |\n"
            "          line1\n"
            "          scripts/foo.sh\n"
            "          line3\n"
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=False)
        assert len(parsed.run_blocks) == 1
        assert "scripts/foo.sh" in parsed.run_blocks[0].content

    def test_extracts_single_line_run(self, module, tmp_path: Path) -> None:
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "jobs:\n  x:\n    steps:\n      - run: bash scripts/foo.sh --flag\n"
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=False)
        assert len(parsed.run_blocks) == 1
        assert "scripts/foo.sh" in parsed.run_blocks[0].content

    def test_does_not_capture_with_block_command(self, module, tmp_path: Path) -> None:
        """`with: command:` parameters are NOT run blocks."""
        wf_path = tmp_path / "wf.yml"
        wf_path.write_text(
            "jobs:\n"
            "  x:\n"
            "    steps:\n"
            "      - uses: ./.github/actions/oneshot\n"
            "        with:\n"
            '          command: \'["node", "scripts/seed.mjs"]\'\n'
        )
        parsed = module.parse_yaml_file(wf_path, tmp_path, is_workflow=False)
        # The `with:` block is not a run block; no run_blocks should be
        # captured.
        assert parsed.run_blocks == []


# ---------------------------------------------------------------------------
# AC #1: the check exits 0 against the live repo on main post-#4077
# ---------------------------------------------------------------------------


class TestRealRepoIsClean:
    """Run the check against the actual repo and assert exit 0.

    This is the live verification of AC #1: "exits 0 on main post-#4077
    (current state has zero violations)". It also runs every PR going
    forward, so a future PR that adds a runner-side script invocation
    without updating paths will fail this test.
    """

    def test_exit_zero_on_real_repo(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            "check-workflow-paths-filter-coverage found violations on "
            "the current tree. Add the missing scripts to the workflow's "
            "on.<trigger>.paths filter, or pass --workflows-dir to "
            "exclude a non-applicable workflow.\n"
            f"stderr:\n{result.stderr}"
        )
