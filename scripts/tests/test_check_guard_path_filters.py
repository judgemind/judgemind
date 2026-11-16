"""Tests for scripts/check_guard_path_filters.py (#5088)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import check_guard_path_filters as cgpf  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

CI_HEAD = """name: CI
on:
  pull_request:
    branches: [main]
jobs:
  detect-changes:
    runs-on: ubuntu-latest
    steps:
      - uses: dorny/paths-filter@v4
        id: changes
        with:
          filters: |
            scripts:
              - 'scripts/**'
              - 'packages/*/tests/**'
            api:
              - 'packages/api/**'
            web:
              - 'packages/web/**'
            scraper:
              - 'packages/scraper-framework/**'
            nlp:
              - 'packages/nlp-pipeline/**'
"""


def make_repo(
    tmp_path: Path,
    jobs: str,
    guards: dict[str, str],
    extra_workflows: dict[str, str] | None = None,
) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(CI_HEAD + jobs)
    for name, body in (extra_workflows or {}).items():
        (wf / name).write_text(body)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name, header in guards.items():
        (scripts / name).write_text("#!/usr/bin/env bash\n" + header + "exit 0\n")
    return tmp_path


def filtered_job(
    guard: str, output: str = "scripts", name: str = "scripts-tests"
) -> str:
    return f"""  {name}:
    needs: detect-changes
    if: needs.detect-changes.outputs.{output} == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: run guard
        run: scripts/{guard}
"""


def unfiltered_job(guard: str, name: str = "repo-guards") -> str:
    return f"""  {name}:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: run guard
        run: scripts/{guard}
"""


def run_main(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = cgpf.main(["--repo-root", str(repo)])
    out = capsys.readouterr()
    return rc, out.out, out.err


# ---------------------------------------------------------------------------
# glob_covers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "scan", "expected"),
    [
        ("scripts/**", "scripts/", True),
        ("scripts/**", "scripts/*.py", True),
        ("scripts/**", "scripts/dispatcher/daemon.py", True),
        ("packages/api/**", "packages/api/src/", True),
        ("packages/scraper-framework/**", "packages/*/src/", False),
        ("packages/*/tests/**", "packages/api/tests/", True),
        ("packages/*/tests/**", "packages/*/src/", False),
        ("infra/**", "infra/terraform/", True),
        ("scripts/*.py", "scripts/", False),
        ("scripts/*.py", "scripts/foo.py", True),
        ("scripts/*.py", "scripts/*.py", True),
        ("scripts/*.py", "scripts/*", False),
        (".github/workflows/deploy-*.yml", ".github/workflows/deploy-*.yml", True),
        (".github/workflows/deploy-*.yml", ".github/", False),
        (
            "packages/web/src/app/**/admin/dispatcher/**",
            "packages/web/src/app/(main)/admin/dispatcher/",
            True,
        ),
        ("packages/web/src/app/**/admin/dispatcher/**", "packages/web/src/app/", False),
        ("Dockerfile.dispatcher", "Dockerfile.dispatcher", True),
        ("'scripts/**'", "scripts/x.sh", True),
        ("**", "anything/at/all/", True),
    ],
)
def test_glob_covers(glob: str, scan: str, expected: bool) -> None:
    assert cgpf.glob_covers(glob, scan) is expected


def test_negated_glob_never_covers() -> None:
    assert not cgpf.covered_by_any(["!scripts/**"], "scripts/")


# ---------------------------------------------------------------------------
# parse_pr_trigger
# ---------------------------------------------------------------------------


def test_pr_trigger_block_with_paths() -> None:
    lines = """on:
  push:
    paths:
      - "infra/**"
  pull_request:
    branches: [main]
    paths:
      - "infra/terraform/**"   # comment
      - 'scripts/x.sh'
jobs:
""".splitlines()
    t = cgpf.parse_pr_trigger(lines)
    assert t.runs_on_pr
    assert t.paths == ["infra/terraform/**", "scripts/x.sh"]


def test_pr_trigger_without_paths_is_unfiltered() -> None:
    t = cgpf.parse_pr_trigger(
        "on:\n  pull_request:\n    branches: [main]\njobs:\n".splitlines()
    )
    assert t.runs_on_pr and t.paths is None


def test_pr_trigger_inline_forms() -> None:
    assert cgpf.parse_pr_trigger(["on: [push, pull_request]"]).runs_on_pr
    assert not cgpf.parse_pr_trigger(["on: push"]).runs_on_pr
    assert not cgpf.parse_pr_trigger(
        ["on:", "  issues:", "    types: [opened]"]
    ).runs_on_pr
    assert not cgpf.parse_pr_trigger(["name: x"]).runs_on_pr


# ---------------------------------------------------------------------------
# End-to-end on synthetic repos
# ---------------------------------------------------------------------------


def test_unfiltered_guard_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, unfiltered_job("check-a.sh"), {"check-a.sh": ""})
    rc, out, _ = run_main(repo, capsys)
    assert rc == 0
    assert "every guard runs" in out


def test_undeclared_guard_in_filtered_job_fails_with_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, filtered_job("check-a.sh"), {"check-a.sh": ""})
    rc, _, err = run_main(repo, capsys)
    assert rc == 1
    assert "scripts/check-a.sh" in err
    assert "job 'scripts-tests', runs on: detect-changes.scripts" in err
    assert "no `# scans:` declaration" in err
    assert re.search(r"^Fix:", err, re.M)
    assert "`repo-guards` job" in err
    assert "# scans: packages/api/src/ scripts/dispatcher/daemon.py" in err


def test_declared_and_covered_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path,
        filtered_job("check-a.sh"),
        {"check-a.sh": "# scans: scripts/ packages/api/tests/\n"},
    )
    rc, _, _ = run_main(repo, capsys)
    assert rc == 0


def test_mis_wired_repo_scanning_guard_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #5088 shape: a packages/*/src guard inside the scripts-filtered job."""
    repo = make_repo(
        tmp_path,
        filtered_job("check-no-unflagged-page-cap.sh"),
        {"check-no-unflagged-page-cap.sh": "# scans: packages/*/src/ scripts/\n"},
    )
    rc, _, err = run_main(repo, capsys)
    assert rc == 1
    assert (
        "declares `# scans: packages/*/src/`, which no invocation's filter covers"
        in err
    )
    assert "scans: scripts/`" not in err  # the covered path is not reported
    assert re.search(r"^Fix:", err, re.M)


def test_union_of_or_gated_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = """  multi:
    needs: detect-changes
    if: needs.detect-changes.outputs.api == 'true' || needs.detect-changes.outputs.web == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: scripts/check-a.sh
"""
    repo = make_repo(
        tmp_path, jobs, {"check-a.sh": "# scans: packages/api/src/ packages/web/src/\n"}
    )
    assert run_main(repo, capsys)[0] == 0


def test_any_testable_maps_to_package_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = filtered_job("check-a.sh", output="any-testable", name="cov")
    repo = make_repo(
        tmp_path, jobs, {"check-a.sh": "# scans: packages/nlp-pipeline/src/\n"}
    )
    assert run_main(repo, capsys)[0] == 0
    (repo / "scripts" / "check-a.sh").write_text(
        "#!/usr/bin/env bash\n# scans: scripts/\n"
    )
    assert run_main(repo, capsys)[0] == 1


def test_one_unfiltered_invocation_is_enough(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = filtered_job("check-a.sh") + unfiltered_job("check-a.sh")
    repo = make_repo(tmp_path, jobs, {"check-a.sh": ""})
    assert run_main(repo, capsys)[0] == 0


def test_each_scan_path_may_be_covered_by_a_different_invocation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = filtered_job("check-a.sh") + filtered_job(
        "check-a.sh", output="api", name="api-guard"
    )
    repo = make_repo(
        tmp_path, jobs, {"check-a.sh": "# scans: scripts/ packages/api/src/\n"}
    )
    assert run_main(repo, capsys)[0] == 0


def test_step_level_if_narrows_an_unfiltered_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = """  mixed:
    needs: detect-changes
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: gated
        if: needs.detect-changes.outputs.api == 'true'
        run: scripts/check-a.sh
      - name: always
        run: scripts/check-b.sh
"""
    repo = make_repo(tmp_path, jobs, {"check-a.sh": "", "check-b.sh": ""})
    rc, _, err = run_main(repo, capsys)
    assert rc == 1
    assert "scripts/check-a.sh" in err
    assert "scripts/check-b.sh" not in err


def test_matrix_step_if_is_not_a_path_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = """  shards:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - shard: python
    steps:
      - name: guard
        if: matrix.shard == 'python'
        run: scripts/check-a.sh
"""
    repo = make_repo(tmp_path, jobs, {"check-a.sh": ""})
    assert run_main(repo, capsys)[0] == 0


def test_workflow_level_pr_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tf = """name: Terraform
on:
  pull_request:
    paths:
      - "infra/terraform/**"
jobs:
  terraform:
    runs-on: ubuntu-latest
    steps:
      - name: hygiene
        run: scripts/check-tf.sh
"""
    repo = make_repo(
        tmp_path,
        "",
        {"check-tf.sh": "# scans: infra/terraform/\n"},
        {"terraform.yml": tf},
    )
    assert run_main(repo, capsys)[0] == 0
    (repo / "scripts" / "check-tf.sh").write_text(
        "#!/usr/bin/env bash\n# scans: infra/\n"
    )
    rc, _, err = run_main(repo, capsys)
    assert rc == 1
    assert "runs on: on.pull_request.paths" in err


def test_non_pr_workflow_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy = """name: Deploy
on:
  push:
    branches: [main]
    paths:
      - "packages/**"
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: bash scripts/check-a.sh
"""
    repo = make_repo(tmp_path, "", {"check-a.sh": ""}, {"deploy.yml": deploy})
    assert run_main(repo, capsys)[0] == 0


def test_comment_lines_are_not_invocations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = """  scripts-tests:
    needs: detect-changes
    if: needs.detect-changes.outputs.scripts == 'true'
    runs-on: ubuntu-latest
    steps:
      # mentions scripts/check-a.sh in prose only
      - run: echo hi
"""
    repo = make_repo(tmp_path, jobs, {"check-a.sh": ""})
    assert run_main(repo, capsys)[0] == 0


def test_python_invocation_forms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = """  scripts-tests:
    needs: detect-changes
    if: needs.detect-changes.outputs.scripts == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: python3 scripts/check-a.py --base origin/main
"""
    repo = make_repo(tmp_path, jobs, {"check-a.py": ""})
    rc, _, err = run_main(repo, capsys)
    assert rc == 1
    assert "scripts/check-a.py" in err


def test_missing_workflows_dir_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(tmp_path, capsys)[0] == 2


def test_read_scans_multiple_lines_and_absent(tmp_path: Path) -> None:
    f = tmp_path / "g.sh"
    f.write_text("#!/usr/bin/env bash\n# scans: a/ b/\n# scans: c.py\n")
    assert cgpf.read_scans(f) == ["a/", "b/", "c.py"]
    f.write_text("#!/usr/bin/env bash\n# nothing\n")
    assert cgpf.read_scans(f) is None
    assert cgpf.read_scans(tmp_path / "missing.sh") is None


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_real_repo_passes() -> None:
    proc = subprocess.run(
        [str(SCRIPTS_DIR / "check-guard-path-filters.sh")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


def test_real_repo_s3_delete_and_page_cap_run_on_src_only_prs() -> None:
    """#5088 AC: both guards run in a job with no detect-changes gate, listed in ci-passed."""
    problems_before = cgpf.check(REPO_ROOT)
    assert problems_before == []
    invs: dict[str, list[cgpf.Invocation]] = {}
    for wf in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for name, inv in cgpf.collect_invocations(wf, REPO_ROOT):
            invs.setdefault(name, []).append(inv)
    for guard in ("check-no-unguarded-s3-delete.sh", "check-no-unflagged-page-cap.sh"):
        assert any(
            cgpf.is_unfiltered(i) and i.workflow == ".github/workflows/ci.yml"
            for i in invs[guard]
        ), guard
        jobs = {i.job for i in invs[guard] if cgpf.is_unfiltered(i)}
        ci_passed = (
            (REPO_ROOT / ".github" / "workflows" / "ci.yml")
            .read_text()
            .split("  ci-passed:", 1)[1]
        )
        assert any(f"      - {j}\n" in ci_passed for j in jobs), jobs


def test_pre_push_scripts_tests_trigger_covers_ci_scripts_filter() -> None:
    """The pre-push scripts/tests trigger must fire on every path CI's `scripts` filter does (#5088)."""
    ci = cgpf.parse_ci_yaml(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    globs = [g for g in ci.filters["scripts"] if g != ".github/workflows/ci.yml"]
    assert globs
    hook = (REPO_ROOT / ".githooks" / "pre-push").read_text()
    m = re.search(
        r"changed_scripts_tests_inputs=\$\(echo \"\$changed_files\" \\\n\s+\| grep -E '([^']+)'",
        hook,
    )
    assert m, "could not find the scripts/tests trigger regex in .githooks/pre-push"
    trigger = re.compile(m.group(1))
    for g in globs:
        sample = g.replace("**", "sub/dir/file.py").replace("*", "x")
        assert trigger.match(sample), (
            f"pre-push trigger misses CI scripts-filter glob {g!r} (sample {sample})"
        )
