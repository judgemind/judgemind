"""Tests for scripts/check_image_path_filters.py (#5060).

Each test builds a synthetic repo (workflow + Dockerfile + files) in a temp
directory and runs the check against it through the shell wrapper, the way
CI and the pre-push hook do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import check_image_path_filters as cipf
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "check-image-path-filters.sh"

DOCKERFILE = """\
FROM python:3.12-slim AS base
WORKDIR /app
COPY packages/cfg /tmp/cfg
COPY packages/app/pyproject.toml ./
COPY --chown=app:app packages/app/src/ ./src/
COPY --from=base /usr/bin/true /usr/bin/true
COPY scripts/reingest.py /opt/tools/reingest.py
COPY ["scripts/rebuild.py", "/opt/tools/rebuild.py"]
COPY scripts/a.sh \\
     scripts/b.sh /opt/tools/
"""

PATHS = [
    "packages/app/**",
    "packages/cfg/**",
    ".github/workflows/deploy-app.yml",
    "scripts/reingest.py",
    "scripts/rebuild.py",
    "scripts/*.sh",
]


def _workflow(paths: list[str], build: str | None = None) -> str:
    items = "".join(f'      - "{p}"\n' for p in paths)
    build = (
        build
        or "docker build \\\n            -f packages/app/Dockerfile \\\n            .\n"
    )
    return (
        "name: Deploy App\n\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "    paths:\n"
        "      # a comment line inside the list\n"
        f"{items}"
        "  workflow_dispatch:\n\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        f"          {build}"
    )


def _repo(tmp_path: Path, paths: list[str], build: str | None = None) -> Path:
    files = {
        "packages/app/Dockerfile": DOCKERFILE,
        "packages/app/pyproject.toml": "[project]\n",
        "packages/app/src/app/__init__.py": "",
        "packages/cfg/pyproject.toml": "[project]\n",
        "packages/cfg/src/cfg/__init__.py": "",
        "scripts/reingest.py": "",
        "scripts/rebuild.py": "",
        "scripts/a.sh": "",
        "scripts/b.sh": "",
        ".github/workflows/deploy-app.yml": _workflow(paths, build),
    }
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WRAPPER), "--repo-root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_live_repo_passes() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "deploy-scraper.yml" in result.stdout


def test_fully_covered_fixture_passes(tmp_path: Path) -> None:
    result = _run(_repo(tmp_path, PATHS))
    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout


def test_unlisted_copied_script_fails_with_fix_block(tmp_path: Path) -> None:
    """The #5060 shape: a COPY'd script missing from on.push.paths."""
    paths = [p for p in PATHS if p != "scripts/reingest.py"]
    result = _run(_repo(tmp_path, paths))
    assert result.returncode == 1
    err = result.stderr
    assert "COPY source 'scripts/reingest.py'" in err
    assert "packages/app/Dockerfile:7" in err
    assert "\nFix:" in err
    assert '      - "scripts/reingest.py"' in err
    assert "# .github/workflows/deploy-app.yml" in err


def test_unlisted_copied_directory_suggests_glob(tmp_path: Path) -> None:
    paths = [p for p in PATHS if p != "packages/cfg/**"]
    result = _run(_repo(tmp_path, paths))
    assert result.returncode == 1
    assert '      - "packages/cfg/**"' in result.stderr


def test_json_form_and_continued_copy_are_checked(tmp_path: Path) -> None:
    paths = [p for p in PATHS if p not in ("scripts/rebuild.py", "scripts/*.sh")]
    result = _run(_repo(tmp_path, paths))
    assert result.returncode == 1
    err = result.stderr
    assert '- "scripts/rebuild.py"' in err
    assert '- "scripts/a.sh"' in err
    assert '- "scripts/b.sh"' in err


def test_dockerfile_itself_must_trigger(tmp_path: Path) -> None:
    paths = ["packages/app/src/**", "packages/app/pyproject.toml"] + PATHS[1:]
    result = _run(_repo(tmp_path, paths))
    assert result.returncode == 1
    assert '- "packages/app/Dockerfile"' in result.stderr


def test_explicit_negation_counts_as_a_decision(tmp_path: Path) -> None:
    paths = [p for p in PATHS if p != "scripts/rebuild.py"] + ["!scripts/rebuild.py"]
    result = _run(_repo(tmp_path, paths))
    assert result.returncode == 0, result.stderr


def test_workflow_without_path_filter_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path, PATHS)
    wf = repo / ".github/workflows/deploy-app.yml"
    wf.write_text(
        "on:\n  push:\n    branches: [main]\njobs:\n  b:\n    steps:\n"
        "      - run: docker build -f packages/app/Dockerfile .\n"
    )
    result = _run(repo)
    assert result.returncode == 0, result.stderr


def test_non_root_build_context_is_a_shape_error(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path, PATHS, build="docker build -f packages/app/Dockerfile packages/app\n"
    )
    result = _run(repo)
    assert result.returncode == 2
    assert "build context" in result.stderr


def test_copy_source_missing_from_repo_is_a_shape_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path, PATHS)
    (repo / "scripts/rebuild.py").unlink()
    result = _run(repo)
    assert result.returncode == 2
    assert "matches no file" in result.stderr


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("packages/app/**", "packages/app/src/x/y.py", True),
        ("scripts/*.sh", "scripts/a.sh", True),
        ("scripts/*.sh", "scripts/sub/a.sh", False),
        ("scripts/dispatcher/**/*.md", "scripts/dispatcher/README.md", True),
        ("scripts/dispatcher/**/*.md", "scripts/dispatcher/a/b/c.md", True),
        (".claude/hooks/preflight*", ".claude/hooks/preflight_x.sh", True),
        ("data-quality-baselines.json", "data-quality-baselinesXjson", False),
        ("scripts/?.py", "scripts/a.py", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert bool(cipf.glob_to_regex(pattern).match(path)) is expected


def test_parse_push_paths_inline_list() -> None:
    text = "on:\n  push:\n    paths: [\"a/**\", 'b.py']\n"
    assert cipf.parse_push_paths(text) == ["a/**", "b.py"]


def test_parse_push_paths_ignores_pull_request_paths() -> None:
    text = (
        "on:\n  pull_request:\n    paths:\n      - 'x/**'\n"
        "  push:\n    branches: [main]\njobs: {}\n"
    )
    assert cipf.parse_push_paths(text) is None


def test_parse_copy_add_url_is_skipped() -> None:
    srcs = cipf.parse_copy_sources(
        "ADD https://example.com/x.tgz /tmp/\nADD local.txt /tmp/\n"
    )
    assert [s.source for s in srcs] == ["local.txt"]
