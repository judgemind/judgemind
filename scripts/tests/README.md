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

### Pattern

```python
import sys
from unittest.mock import MagicMock

# 1. Create mock modules BEFORE importing the script under test.
mock_psycopg = MagicMock()
mock_structlog = MagicMock()
mock_structlog.get_logger.return_value = MagicMock()

# 2. Inject them into sys.modules.
sys.modules["psycopg"] = mock_psycopg
sys.modules["structlog"] = mock_structlog

# For packages with sub-modules, mock the full dotted path:
mock_ingestion = MagicMock()
sys.modules["ingestion"] = mock_ingestion
sys.modules["ingestion.llm_providers"] = MagicMock()
sys.modules["ingestion.ruling_formatter"] = MagicMock()

# 3. Now import the script — it picks up the mocks.
import my_script  # noqa: E402
```

### Key details

- **Order matters.** The `sys.modules` entries must exist before the script's
  `import` statements execute. Do this at module level in the test file, not
  inside a test function or fixture.
- **Sub-modules need separate entries.** `from ingestion.ruling_formatter import X`
  requires both `sys.modules["ingestion"]` and
  `sys.modules["ingestion.ruling_formatter"]` to be set.
- **Restore if needed.** If you want to be defensive, save any pre-existing
  `sys.modules` entries and restore them in a teardown. See
  `test_backfill_ruling_html.py` for an example of this pattern.
- **Use `patch.dict(sys.modules, ...)` for function-level mocking.** When the
  script uses lazy imports inside functions (not at module level), you can mock
  per-test with `patch.dict`. See `test_dev_db_query_runner.py`'s
  `TestRunQuery` class for this approach.

## Examples

- **`test_backfill_ruling_html.py`** — Module-level `sys.modules` mocking for
  psycopg, anthropic, structlog, and ingestion sub-modules. The script imports
  all of these at the top level.
- **`test_dev_db_query_runner.py`** — Function-level `patch.dict(sys.modules, ...)`
  for psycopg, because the script uses a lazy import inside `run_query()`.

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
