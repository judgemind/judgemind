# Scripts Tests

Tests for standalone scripts in `scripts/`.

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

Scripts use `# venv: <package>` header comments instead of `ensure_venv()`.
The `_VENV_HELPER_SKIP=1` environment variable is set in CI for legacy
compatibility.

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
