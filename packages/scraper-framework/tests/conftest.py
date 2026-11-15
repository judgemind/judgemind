"""Shared fixtures for scraper-framework tests.

Performance optimisation (see GitHub issues #66, #1207, #1219):
reading a PDF's text layer with pdfplumber (``llm_extract._read_pdf_pages``,
the one reader every scraper and the worker use since #5010) takes ~2 s
per call on a multi-page PDF.  In "full run" tests the mock HTTP layer returns
the *same* PDF bytes for every department link (300+ for CC, 33 for OC, 20 for
Fresno, 17 for Riverside), so parsing is repeated hundreds of times for the
same content.

Phase 1 (#1207) added a per-worker in-memory LRU cache.  Phase 2 (#1219)
replaces it with a **disk-based, cross-worker cache** so that all pytest-xdist
workers share extracted text.  The cache uses ``tmp_path_factory`` to locate
the shared temp root (the parent of per-worker temp dirs).  Each entry is
keyed by the SHA-256 hash of the input PDF bytes; the first worker to parse a
given PDF writes the result to disk and all subsequent workers (including
from other xdist processes) read from it.

No external dependencies are needed — the cache uses stdlib ``hashlib`` and
``pathlib``.  Race conditions between workers writing the same key are harmless
because the output is deterministic (same function + same bytes -> same text).

Additionally, ``time.sleep`` in the retry module is replaced with a no-op so
that retry-backoff tests (which sleep 2 s + 4 s = 6 s by default) complete
instantly.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion import llm_extract as _llm_extract

# --- BEGIN git-env-scrub (#5058) ---
# Edit scripts/tests/conftest.py, then copy this block verbatim into every
# other conftest.py that has it. scripts/check-test-git-env-scrub.sh fails
# when the copies differ.
#
# `git push` (pre-push hook), `git rebase --exec`, `git bisect run` and `!`
# aliases run their command with GIT_DIR (and, in a linked worktree, more
# GIT_* vars) exported. A test that runs `git init` / `commit` / `config` in
# a scratch directory inherits them and writes to THAT repository instead:
# in #5045 a push set core.bare=true in the shared .git and moved main. This
# session fixture removes every variable `git rev-parse --local-env-vars`
# lists, and points git's global config at a throwaway file so a test cannot
# write the user's ~/.gitconfig either. Both are restored after the session.
_GIT_ENV_SCRUB_GITCONFIG = """\
[user]
\tname = Judgemind Test
\temail = test@example.com
[commit]
\tgpgsign = false
[tag]
\tgpgsign = false
[safe]
\tdirectory = *
"""


def _git_env_scrub_var_names() -> list[str]:
    """Return git's repository env var names; [] when git is not installed."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return []  # No git binary, so no test can write to a repository.
    names = result.stdout.split()
    if result.returncode != 0 or "GIT_DIR" not in names:
        raise RuntimeError(
            "git-env-scrub (#5058): `git rev-parse --local-env-vars` did not "
            f"list GIT_DIR (exit {result.returncode}): {result.stderr.strip()}\n"
            "Fix:\n"
            "  git rev-parse --local-env-vars   # must print GIT_DIR etc."
        )
    return names


@pytest.fixture(autouse=True, scope="session")
def _git_env_scrub(tmp_path_factory: pytest.TempPathFactory) -> object:
    """Unset git's repository env vars and pin global git config (#5058)."""
    gitconfig = tmp_path_factory.mktemp("git-env-scrub") / "gitconfig"
    gitconfig.write_text(_GIT_ENV_SCRUB_GITCONFIG)
    with pytest.MonkeyPatch.context() as mp:
        for name in _git_env_scrub_var_names():
            mp.delenv(name, raising=False)
        mp.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


# --- END git-env-scrub (#5058) ---


# --- BEGIN gh-isolate (#5071) ---
# Edit scripts/tests/conftest.py, then copy this block verbatim into every
# other conftest.py that has it. scripts/check-test-gh-isolate.sh fails when
# the copies differ or when a test root has no copy.
#
# A test that reaches the real `gh` acts on real GitHub with the developer's
# login: in #5071 a dispatcher test ran the real sweep-completed-parents.sh,
# which closed an open issue and posted comments. This session fixture cuts
# every test off from real GitHub:
#   * GH_CONFIG_DIR points at an empty directory (no stored login);
#   * GH_TOKEN, GITHUB_TOKEN, GH_ENTERPRISE_TOKEN and GITHUB_ENTERPRISE_TOKEN
#     are removed;
#   * a `gh` shim goes first on PATH. It never touches the network: it prints
#     "real gh called from a test" to stderr, records the call, appends it to
#     $GH_ISOLATE_LOG when that is set, and exits 4 (gh's "not authenticated"
#     code).
# A test that needs gh installs its own stub (a fake `gh` prepended to PATH,
# or a mocked subprocess call); that stub wins over the shim. Everything is
# restored after the session.
#
# A shim hit FAILS the test that made it (#5075): a test that falls through
# to the shim only exercises a "gh exited 4" branch, not the behaviour it
# claims to test. After each test, _gh_isolate_check fails it ("real gh
# called from a test", the test id and each gh command) when the shim
# recorded a call; a hit outside any test fails the session teardown. A test
# that calls the shim on purpose (a test of this block) requests the
# gh_shim_allowed fixture.
_GH_ISOLATE_TOKEN_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_GH_ISOLATE_SHIM = """\
#!/bin/sh
# gh shim from the gh-isolate test fixture (#5071). Never calls GitHub.
echo "FAIL: real gh called from a test (#5071): gh $*" >&2
echo "Fix: stub gh in this test (put a fake gh first on PATH, or mock the call)." >&2
printf '%s\\tgh %s\\n' "${PYTEST_CURRENT_TEST:-?}" "$*" >>'@HITS@'
if [ -n "${GH_ISOLATE_LOG:-}" ]; then
    printf '%s\\tgh %s\\n' "${PYTEST_CURRENT_TEST:-${GH_ISOLATE_TEST:-?}}" "$*" >>"$GH_ISOLATE_LOG"
fi
exit 4
"""
_GH_ISOLATE_FIX = (
    "Fix: stub gh in this test: put a fake gh first on PATH (the fake_gh "
    "fixture in scripts/dispatcher/tests/conftest.py) or mock the subprocess "
    "call, and assert the gh calls the code should make. A test that calls "
    "the shim on purpose requests the gh_shim_allowed fixture."
)


def _gh_isolate_state(config: pytest.Config) -> dict:
    """Per-session shim state, shared by every conftest copy in the process."""
    state = getattr(config, "_gh_isolate_state", None)
    if state is None:
        state = {"hits": None, "offset": 0}
        config._gh_isolate_state = state  # type: ignore[attr-defined]
    return state


def _gh_isolate_new_hits(state: dict) -> list[str]:
    """Return the gh commands the shim recorded since the last call."""
    if state["hits"] is None or not os.path.exists(state["hits"]):
        return []
    with open(state["hits"], encoding="utf-8", errors="replace") as fh:
        fh.seek(state["offset"])
        new = fh.read()
        state["offset"] = fh.tell()
    return [line.split("\t", 1)[-1] for line in new.splitlines() if line]


@pytest.fixture(autouse=True, scope="session")
def _gh_isolate(request: pytest.FixtureRequest) -> object:
    """Cut the test session off from real GitHub (#5071)."""
    state = _gh_isolate_state(request.config)
    if state["hits"] is not None:  # another conftest copy already did it
        yield
        return
    root = request.getfixturevalue("tmp_path_factory").mktemp("gh-isolate")
    bin_dir = root / "bin"
    config_dir = root / "config"
    bin_dir.mkdir()
    config_dir.mkdir()
    state["hits"] = str(root / "hits.log")
    shim = bin_dir / "gh"
    shim.write_text(_GH_ISOLATE_SHIM.replace("@HITS@", state["hits"]))
    shim.chmod(0o755)
    log = os.environ.get("GH_ISOLATE_LOG") or str(root / "calls.log")
    with pytest.MonkeyPatch.context() as mp:
        for name in _GH_ISOLATE_TOKEN_VARS:
            mp.delenv(name, raising=False)
        mp.delenv("GH_ISOLATE_PID", raising=False)  # a shell test's, not ours
        mp.setenv("GH_CONFIG_DIR", str(config_dir))
        mp.setenv("GH_ISOLATE_LOG", log)
        mp.setenv("GH_NO_UPDATE_NOTIFIER", "1")
        mp.setenv("GH_PROMPT_DISABLED", "1")
        mp.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        yield
    stray = _gh_isolate_new_hits(state)
    if stray:
        raise RuntimeError(
            "real gh called from a test (#5075): the gh shim was called "
            "outside any test (a session or module fixture):\n"
            + "".join(f"  {cmd}\n" for cmd in stray)
            + _GH_ISOLATE_FIX
        )


@pytest.fixture
def gh_shim_allowed() -> None:
    """Request this in a test that calls the gh shim on purpose (#5075)."""


@pytest.fixture(autouse=True)
def _gh_isolate_check(request: pytest.FixtureRequest, _gh_isolate: object) -> object:
    """Fail the test when it (or a fixture it used) hit the gh shim (#5075)."""
    yield
    hits = _gh_isolate_new_hits(_gh_isolate_state(request.config))
    if hits and "gh_shim_allowed" not in request.fixturenames:
        pytest.fail(
            f"real gh called from a test (#5075): {request.node.nodeid} ran "
            f"{len(hits)} gh command(s) that reached the gh shim:\n"
            + "".join(f"  {cmd}\n" for cmd in hits)
            + _GH_ISOLATE_FIX,
            pytrace=False,
        )


# --- END gh-isolate (#5071) ---

# ---------------------------------------------------------------------------
# Disk-based cross-worker PDF text cache
# ---------------------------------------------------------------------------

# Module-level cache dir, set by the ``_pdf_cache_dir`` session fixture.
_pdf_cache_dir: Path | None = None


def _make_disk_cached(
    real_fn: Callable[[bytes], list[_llm_extract._PdfPage]],
) -> Callable[[bytes], list[_llm_extract._PdfPage]]:
    """Wrap the PDF page reader with a disk-backed cache.

    On cache miss the real function is called and the pages are written to
    ``<cache_dir>/pdf_pages_<sha256>.json``.  On cache hit the file is read
    directly.  An unreadable PDF raises, uncached, as the reader does.
    """

    def _cached(pdf_bytes: bytes) -> list[_llm_extract._PdfPage]:
        cache_dir = _pdf_cache_dir
        if cache_dir is None:
            # Fixture not yet initialised — fall back to uncached call
            return real_fn(pdf_bytes)

        key = hashlib.sha256(pdf_bytes).hexdigest()
        cache_file = cache_dir / f"pdf_pages_{key}.json"

        # Fast path: cache hit
        if cache_file.exists():
            return [
                _llm_extract._PdfPage(text, scanned)
                for text, scanned in json.loads(cache_file.read_text(encoding="utf-8"))
            ]

        # Slow path: call the real reader, write to cache
        result = real_fn(pdf_bytes)

        # Atomic-ish write: write to a PID-unique temp file then rename.
        # Using os.getpid() ensures each xdist worker writes to its own
        # temp file, avoiding a race where two workers clobber the same
        # .tmp path.  On POSIX, os.rename atomically replaces the target
        # so the last writer wins — both produce identical content.
        tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps([[p.text, p.scanned] for p in result]), encoding="utf-8")
        try:
            os.rename(tmp, cache_file)
        except OSError:
            # Another worker created the file first — clean up.
            tmp.unlink(missing_ok=True)

        return result

    return _cached


@pytest.fixture(autouse=True, scope="session")
def _setup_pdf_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Create a shared PDF cache directory for all xdist workers.

    With pytest-xdist, ``tmp_path_factory.getbasetemp()`` returns a
    per-worker dir like ``/tmp/pytest-xxx/worker-gw0``.  Its parent
    (``/tmp/pytest-xxx/``) is shared across all workers — we create a
    ``pdf_cache`` subdirectory there.

    Without xdist, ``getbasetemp()`` returns the root directly (e.g.
    ``/tmp/pytest-xxx/``).  In that case we still create ``pdf_cache``
    inside it — there's only one process, so sharing isn't needed but
    the cache still speeds up repeated calls within a single session.
    """
    global _pdf_cache_dir  # noqa: PLW0603

    base = tmp_path_factory.getbasetemp()
    # xdist workers: base = /tmp/pytest-xxx/worker-gwN/ -> parent is shared
    # no xdist: base = /tmp/pytest-xxx/ -> use it directly
    shared_root = base.parent if base.name.startswith("worker-") else base
    cache_dir = shared_root / "pdf_cache"
    cache_dir.mkdir(exist_ok=True)

    _pdf_cache_dir = cache_dir
    yield
    _pdf_cache_dir = None


@pytest.fixture(autouse=True, scope="session")
def _cache_pdf_text_extraction(
    _setup_pdf_cache_dir: None,
) -> Generator[None, None, None]:
    """Wrap the PDF page reader with a cross-worker disk cache.

    Uses ``unittest.mock.patch`` instead of ``monkeypatch`` because
    ``monkeypatch`` is limited to function scope.  Every PDF text path (the
    scrapers' ``scraper_pdf_text``, the worker's ``extract_pdf_text``)
    reads pages through ``llm_extract._read_pdf_pages`` (#5010).
    """
    with patch.object(
        _llm_extract, "_read_pdf_pages", _make_disk_cached(_llm_extract._read_pdf_pages)
    ):
        yield


@pytest.fixture(autouse=True, scope="session")
def _disable_llm_s3_cache() -> Generator[None, None, None]:
    """Prevent LLM extraction cache from hitting real S3 during tests.

    The ``_LlmCache`` in ``llm_extractor.py`` uses ``S3_CACHE_DIR`` to
    locate cached results.  If this env var is set in the developer's shell
    (e.g. for local rebuilds), tests that call ``extract_from_pdf`` can
    hit stale cached results instead of exercising the mocked code paths.

    We patch the ``_get_cache_s3_client`` to return a mock whose get/put
    methods always raise, so the ``_LlmCache.get()`` returns ``None``
    (its except branch) and ``_LlmCache.put()`` logs a warning.  The
    ``_LlmCache`` class is NOT replaced, preserving coverage measurement.
    """
    broken_s3 = MagicMock()
    broken_s3.get_object.side_effect = Exception("no S3 in tests")
    broken_s3.put_object.side_effect = Exception("no S3 in tests")
    with patch(
        "framework.llm_extractor.LlmExtractor._get_cache_s3_client",
        staticmethod(lambda: broken_s3),
    ):
        yield


@pytest.fixture(autouse=True, scope="session")
def _optional_llm_extraction() -> Generator[None, None, None]:
    """Run the suite's ingestion workers in ``optional`` extraction mode.

    A deployed worker defaults to ``required`` mode: it builds the LLM
    extractors at startup and refuses to start without them (#4953).  The
    unit tests have no LLM keys and stub the extractors they need, so they
    use the explicit opt-in that local regex-only runs use.  Tests of the
    required mode set ``JUDGEMIND_EXTRACTION`` themselves.
    """
    with patch.dict(os.environ, {"JUDGEMIND_EXTRACTION": "optional"}):
        yield


@pytest.fixture(autouse=True, scope="session")
def _block_telemetry_db_writes() -> Generator[None, None, None]:
    """Prevent any test from accidentally writing to ``telemetry.scraper_runs``.

    Two layers of protection applied for the full test session:

    1. ``DATABASE_URL`` is patched to the empty string so that
       ``framework.runner._connect_db`` short-circuits its early-return guard
       (``if not database_url: return None``).
    2. ``framework.runner._connect_db`` is patched directly to return ``None``
       in case any code path reads DATABASE_URL before the env patch takes
       effect, or if a future refactor skips the env-var check.

    Tests that need a real (mock) DB connection apply their own
    ``patch("framework.runner._connect_db", ...)`` which takes precedence
    over this session-level patch within their context-manager scope —
    the stacked patches in ``TestConnectDb`` (test_runner.py:850-879) and
    the ``Test*RecordScraperRun*`` / ``TestRecordScraperException*`` classes
    are unaffected.
    """
    with patch.dict(os.environ, {"DATABASE_URL": ""}):
        with patch("framework.runner._connect_db", return_value=None):
            yield


@pytest.fixture(autouse=True, scope="session")
def _fast_retry_sleeps() -> Generator[None, None, None]:
    """Replace ``time.sleep`` in the retry module with a no-op.

    Tests that exercise retry paths (e.g. HTTP 503 -> retry -> fail) would
    otherwise sleep 2 s + 4 s = 6 s per test due to exponential backoff.
    The retry *logic* (attempt counting, exception propagation) is still
    fully exercised — only the wall-clock wait is removed.
    """
    with patch("framework.retry.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _clear_roster_cache() -> None:
    """Clear the roster name cache before every test.

    ``_get_roster_names()`` caches results per court_id in a module-level
    dict.  Without this fixture, a test that calls ``resolve_judge()`` can
    pollute the cache for later tests running in the same process (or xdist
    worker), causing ``fetchone`` side-effect sequences to be consumed in
    an unexpected order.
    """
    from ingestion.db import clear_roster_cache

    clear_roster_cache()


@pytest.fixture(autouse=True)
def _skip_judge_votes_on_mock_connections() -> Generator[None, None, None]:
    """Skip the worker's ``record_judge_votes`` on a mocked connection.

    Every worker write settles its documents' judge votes before it
    commits (#4966).  Tests that drive the worker over a mocked psycopg
    connection feed ``fetchone`` / ``fetchall`` side effects in a fixed
    order that predates those queries.  On a ``Mock`` connection the call
    is a no-op; on a real connection (the ``*_pg`` tests) it runs.  Tests
    that check the call itself patch ``ingestion.worker.record_judge_votes``
    themselves, which takes precedence.
    """
    from unittest.mock import Mock

    from ingestion import db as db_mod

    real = db_mod.record_judge_votes

    def _record(conn: object, *args: object, **kwargs: object) -> set[str]:
        if isinstance(conn, Mock):
            return set()
        return real(conn, *args, **kwargs)  # type: ignore[arg-type]

    with patch("ingestion.worker.record_judge_votes", side_effect=_record):
        yield


@pytest.fixture(autouse=True)
def _mock_enrichment_engine() -> Generator[None, None, None]:
    """Auto-mock ``EnrichmentEngine`` in the ingestion worker for all tests.

    The enrichment engine performs DB queries via the psycopg connection.
    Existing tests mock psycopg with carefully ordered ``fetchone`` side
    effects that don't account for enrichment queries.  This fixture
    patches the ``EnrichmentEngine`` class in the worker module so that
    ``enrich()`` returns a no-op result (exact case match, no judge
    changes, no flags) — preserving all existing test behaviour.

    Tests that specifically test enrichment wiring (``test_enrichment_wiring.py``)
    apply their own patch which takes precedence over this autouse fixture.
    """
    from framework.enrichment import CaseMatch, EnrichmentResult, JudgeResolution

    def _noop_enrich(**kwargs: object) -> EnrichmentResult:
        case_number = kwargs.get("case_number", "")
        return EnrichmentResult(
            case_match=CaseMatch(
                case_id="",
                case_number=str(case_number),
                match_type="exact",
                confidence=1.0,
            ),
            judge_resolution=JudgeResolution(
                judge_id=None,
                canonical_name=None,
                match_type="new",
                confidence=0.0,
            ),
            party_resolutions=[],
            flags=[],
        )

    from unittest.mock import MagicMock

    mock_engine = MagicMock()
    mock_engine.enrich.side_effect = _noop_enrich

    with patch("ingestion.worker.EnrichmentEngine", return_value=mock_engine):
        yield
