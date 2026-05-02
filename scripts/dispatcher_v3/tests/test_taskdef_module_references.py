"""Assert every task-def ``command = ["python", "-m", "<mod>"]`` resolves.

The dispatcher-v3 ECS task definitions in
``infra/terraform/modules/dispatcher-v3-task-defs/main.tf`` launch a
shared Docker image with different argv per role:

  - launcher        → ``python -m dispatcher_v3.launcher``
  - task-runner     → ``python -m dispatcher_v3.agent_runner``
  - diagnoser       → ``python -m dispatcher_v3.diagnoser_runner``
  - scheduled-skill → ``python -m dispatcher_v3.scheduled_skill_runner``

If a Terraform argv references a Python module that does not exist on
the deployed image, the ECS task fails at container start with
``ModuleNotFoundError: No module named '<mod>'`` — the same failure
mode that motivated this test (issue #3972: F2's diagnoser task-def
``command`` named ``dispatcher_v3.diagnoser_runner`` weeks before the
module landed on main).

This is the boundary where ``docker build`` doesn't help (the build
succeeds — the missing module isn't reached until container start) and
``terraform plan`` doesn't help either (the plan shows a syntactically
valid ``command`` list). A small static check on the Terraform source
catches the entire class of "argv references unimportable Python
module" drift in CI, before deploy.

The check is intentionally narrow: it scans for the exact
``["python", "-m", "<mod>"]`` shape used in v3 task-defs. If we ever
add task-defs with a different argv shape (e.g. wrapping a shell
script), this regex won't match them and they pass through this test
silently. That's by design — only the ``-m <mod>`` shape has the
"deployed but crashloops at import" failure mode.

Why a regex on the rendered Terraform source rather than parsing the
HCL? The Terraform AST is not stable across versions and pulling in a
parser dependency for a five-line check has worse upkeep than a tight
regex. The pattern is anchored on the literal HCL string ``command =
[ ... "python" , "-m" , "<mod>" ...`` which is unambiguous in the
single file we scan.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

# Path resolved from this test file's location so the test runs from
# any cwd: pytest's collection cwd, IDE runners, and CI all work.
_REPO_ROOT = Path(__file__).resolve().parents[3]
TASKDEFS = _REPO_ROOT / "infra/terraform/modules/dispatcher-v3-task-defs/main.tf"

# Regex matches the literal HCL form
#   command = ["python", "-m", "<module>"]
# allowing arbitrary whitespace between tokens so a future formatter
# tweak doesn't break us silently. The ``re.MULTILINE`` flag is mostly
# defensive — the pattern doesn't span lines today, but a future
# multi-line ``command =`` block with the same argv shape should still
# match.
PATTERN = re.compile(
    r'command\s*=\s*\[\s*"python"\s*,\s*"-m"\s*,\s*"([^"]+)"',
    re.MULTILINE,
)


def test_taskdef_commands_resolve() -> None:
    """Every ``python -m <mod>`` argv in main.tf is an importable module.

    Catches the class of bug where a Terraform argv references a
    Python module that does not exist on main — the deployed ECS task
    crashloops at container start with ``ModuleNotFoundError``. See
    issue #3972 for the F2 diagnoser drift that motivated this guard.
    """
    text = TASKDEFS.read_text()
    modules = sorted(set(PATTERN.findall(text)))
    assert modules, (
        f"regex matched zero ``python -m <mod>`` invocations in {TASKDEFS} — "
        "either the pattern is broken or the task-def file no longer uses "
        "the python -m argv shape. Update PATTERN to match the new shape."
    )
    missing = []
    for m in modules:
        try:
            importlib.import_module(m)
        except ImportError as exc:
            missing.append(f"{m}: {exc}")
    assert not missing, (
        "Task-def references unimportable modules:\n  - " + "\n  - ".join(missing)
    )
