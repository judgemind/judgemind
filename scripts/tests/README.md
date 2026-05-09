# Scripts Tests

Tests for standalone scripts in `scripts/`.

## CI Execution

The `scripts-tests` CI job auto-discovers and runs every executable
`scripts/tests/*.sh` under a single loop. The runner logic lives in
`scripts/run-scripts-tests.sh` (called from `.github/workflows/ci.yml`,
job `scripts-tests`, step `Run all scripts/tests shell tests`). Adding a
new shell test is just `touch + chmod +x` — no ci.yml edit required.

### Shared helpers (underscore-prefixed files)

Files whose basename starts with `_` (e.g.
`_guard_self_match_helpers.sh`) are treated as **shared helpers** that
peer tests `source` — they are never executed as tests by the runner.
Use this naming convention for any sourced library file under
`scripts/tests/`. See `scripts/run-scripts-tests.sh` and the unit test
`test_scripts_tests_runner.sh` for the enforced behavior (#2559).

### Deferred tests (dedicated CI jobs)

A small set of tests are intentionally **skipped** by the scripts-tests
auto-discovery loop because they already run in a dedicated CI job with a
narrower path filter. Running them in both places is wasted CI minutes.

The skip list is maintained via the `SKIP_TESTS` env var on the
`Run all scripts/tests shell tests` step. Current entries:

| Test | Dedicated job | Trigger | Rationale |
|---|---|---|---|
| `test_pre_push.sh` | `githooks-pre-push-test` | `.githooks/**`, `scripts/tests/test_pre_push.sh`, `.github/workflows/ci.yml` | ~32s. Originally added to scripts-tests via #2505 auto-discovery; caused a +112% duration regression (#2536). |

When adding a new "deferred" entry, make sure the dedicated job's path
filter covers every file that the test exercises — otherwise relevant
changes will no longer run the test in any job.

## CI Environment

The `scripts-tests` CI job runs in a **minimal environment** with only these
packages installed:

- **pytest** (test runner)
- **boto3** (AWS SDK — used by some scripts)
- **Python stdlib**

Notably, these packages are **not available** in CI:

- `psycopg` (database driver)
- `anthropic` (LLM client)
- `structlog` (structured logging)
- Any packages from `packages/scraper-framework/` (e.g. `ingestion.*`)

Scripts use `# venv: <package>` header comments and are run via
`scripts/run-py.sh`, which reads the header and activates the correct venv.

## Mocking `sys.modules` for unavailable packages

If the script under test imports a package that is not available in CI, you
must mock that package in `sys.modules` **before** importing the script.
Importing at module level without mocking will raise `ModuleNotFoundError` in
CI even if it works locally (where those packages are installed).

**Use the `mock_sys_modules` context manager from `_mock_helpers.py`** —
it installs the mocks before the import and restores `sys.modules` after,
so a single forgotten restore can't leak `MagicMock` entries into unrelated
tests collected later in the same pytest run (#4426). The helper is the
canonical pattern as of #4430.

### Pattern (mock_sys_modules — current)

```python
import os
import sys
from unittest.mock import MagicMock

# Add scripts/ to sys.path so the script-under-test imports cleanly, and
# scripts/tests/ to sys.path so the helper module is reachable as
# ``tests._mock_helpers``.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules  # noqa: E402

# When the script-under-test calls e.g. ``structlog.get_logger`` at module
# load (typical for ``framework.logging.configure_structlog``), pre-seed
# the attribute on a caller-built mock and pass the mapping form. Modules
# that need no attribute setup can be passed bare via the iterable form
# (``mock_sys_modules(["boto3", "psycopg"])``) — each name then gets a
# fresh ``MagicMock``.
_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()

with mock_sys_modules(
    {
        "psycopg": MagicMock(),
        "structlog": _mock_structlog,
        "framework": MagicMock(),
        "framework.logging": MagicMock(),
        # Sub-modules need separate entries — ``from ingestion.ruling_formatter
        # import X`` requires ``sys.modules["ingestion.ruling_formatter"]`` to
        # be a mock too, not just ``sys.modules["ingestion"]``.
        "ingestion": MagicMock(),
        "ingestion.ruling_formatter": MagicMock(),
    }
):
    import my_script as _script  # noqa: E402
```

### Key details

- **Order matters.** The `sys.modules` entries must exist before the script's
  `import` statements execute, which is why the import lives inside the
  `with` block. Do not move the import outside the context manager.
- **Sub-modules need separate entries.** `from ingestion.ruling_formatter
  import X` requires both `sys.modules["ingestion"]` and
  `sys.modules["ingestion.ruling_formatter"]` to be set — pass each as a
  separate key in the mapping.
- **Restoration is automatic.** `mock_sys_modules.__exit__` restores
  pre-existing `sys.modules` entries and deletes entries that did not exist
  before the `with` block — even if the import raises. This is the
  invariant pinned by `test_scripts_tests_isolation.py` (#4426).
- **The script's module-level bindings still see the mock.** When the
  script does `from framework.logging import configure_structlog`, the
  symbol `configure_structlog` is bound in the script's namespace at
  import time — which happens inside the `with` block, so it captures
  the mock. Restoring `sys.modules` after the import does NOT rebind the
  script's already-imported globals.
- **Use `patch.dict(sys.modules, ...)` for function-level mocking.** When the
  script uses lazy imports inside functions (not at module level), you can mock
  per-test with `patch.dict`. See `test_dev_db_query_runner.py`'s
  `TestRunQuery` class for this approach.

### Legacy pattern (manual save/restore — superseded)

Pre-#4430 test files maintained their own save/replay boilerplate around
the import:

```python
_modules_to_mock = {"structlog": MagicMock(), ...}
_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import my_script  # noqa: E402

for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]
```

This pattern still works correctly when written as shown above, but a
single forgotten restore loop pollutes `sys.modules` for every later
test (#4426). New test files should use `mock_sys_modules` instead — it
collapses the ~15 lines above to a single `with` block and makes the
restore unforgeable.

## Examples

- **`test_audit_correctly_labeled_s3_orphans.py`** — Module-level
  `mock_sys_modules` for boto3, psycopg, structlog, and framework
  sub-modules. The script imports all of these at the top level.
- **`test_dev_db_query_runner.py`** — Function-level `patch.dict(sys.modules, ...)`
  for psycopg, because the script uses a lazy import inside `run_query()`.
- **`test_mock_helpers.py`** — Unit tests for the `mock_sys_modules`
  helper itself; shows the iterable-of-names form, the mapping form, the
  restore-on-exception path, and the nested-usage path.

## Test durations baseline

Baseline captured 2026-04-25 via `pytest --durations=20 scripts/tests/`.

### Pytest shard durations

Top-5 slowest tests (all at 0.30s; total suite wall-clock ~6.3s across 737 passed, 23 skipped):

| Duration | Test |
|---|---|
| 0.30s | `scripts/tests/test_agent_status.py::TestParseNdjsonFile::test_multiple_tool_calls` |
| 0.30s | `scripts/tests/test_agent_status.py::TestParseNdjsonFile::test_duration_computed` |
| 0.30s | `scripts/tests/test_agent_status.py::TestParseNdjsonFile::test_tool_use_extracted` |
| 0.30s | `scripts/tests/test_agent_status.py::TestParseNdjsonFile::test_deduplicates_tool_use_ids` |
| 0.30s | `scripts/tests/test_agent_status.py::TestParseNdjsonFile::test_token_usage_accumulated` |

### Shell shard durations

The shell shard (~410-450s total) is dominated by two tests:
`test_agent_runner_entrypoint.sh` (~275s) and
`test_check_dispatcher_image_versions.sh` (~58s). These are integration tests
that should keep gating PRs. The sharding rationale (splitting the original
single `scripts-tests` job that exceeded 600s) is captured in
`.github/workflows/ci.yml` lines 596-602 and issue #3307.

If a future `/audit` flags `scripts-tests` again, re-run
`pytest --durations=20 scripts/tests/` and compare against this baseline to
identify which tests regressed.
