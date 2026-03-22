"""Shared fixtures for scraper-framework tests.

Performance optimisation (see GitHub issues #66, #1207, #1219):
``_extract_pdf_text`` / ``extract_pdf_text`` uses pdfplumber which takes ~2 s
per call on a multi-page PDF.  In "full run" tests the mock HTTP layer returns
the *same* PDF bytes for every department link (300+ for CC, 33 for OC, 20 for
Fresno, 17 for Riverside), so parsing is repeated hundreds of times for the
same content.

Phase 1 (#1207) added a per-worker in-memory LRU cache.  Phase 2 (#1219)
replaces it with a **disk-based, cross-worker cache** so that all pytest-xdist
workers share extracted text.  The cache uses ``tmp_path_factory`` to locate
the shared temp root (the parent of per-worker temp dirs).  Each entry is
keyed by the combination of the extraction function's ``__qualname__`` and the
SHA-256 hash of the input PDF bytes; the first worker to parse a given PDF
writes the result to disk and all subsequent workers (including from other
xdist processes) read from it.

Including the function identifier in the key prevents collisions between
different extractors called on the same PDF bytes (e.g. ``_extract_pdf_text``
vs ``_extract_oc_pdf_text``).

No external dependencies are needed — the cache uses stdlib ``hashlib`` and
``pathlib``.  Race conditions between workers writing the same key are harmless
because the output is deterministic (same function + same bytes -> same text).

Additionally, ``time.sleep`` in the retry module is replaced with a no-op so
that retry-backoff tests (which sleep 2 s + 4 s = 6 s by default) complete
instantly.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from courts.ca import cc_tentatives as _cc
from courts.ca import fresno_tentatives as _fresno
from courts.ca import oc_tentatives as _oc
from courts.ca import pdf_link_scraper as _pls
from courts.ca import riverside_tentatives as _riv
from courts.ca import sc_tentatives as _sc
from courts.ca import ventura_tentatives as _ven

# ---------------------------------------------------------------------------
# Backfill module — imported lazily for caching its _extract_oc_pdf_text
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts")


def _import_backfill_oc() -> ModuleType | None:
    """Import the backfill_oc_ruling_text script as a module.

    Returns ``None`` if the script is not found (e.g. running tests from
    a partial checkout).  The caller should skip patching in that case.
    """
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    try:
        return importlib.import_module("backfill_oc_ruling_text")
    except (ImportError, ModuleNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Disk-based cross-worker PDF text cache
# ---------------------------------------------------------------------------

# Module-level cache dir, set by the ``_pdf_cache_dir`` session fixture.
_pdf_cache_dir: Path | None = None


def _make_disk_cached(
    real_fn: Callable[[bytes], str],
) -> Callable[[bytes], str]:
    """Wrap a PDF extraction function with a disk-backed cache.

    On cache miss the real function is called and the result is written to
    ``<cache_dir>/<fn_qualname>_<sha256>.txt``.  On cache hit the file is
    read directly.

    The cache key includes ``real_fn.__qualname__`` so that different
    extractors called on the same PDF bytes get separate cache entries.
    Without this, e.g. ``_extract_pdf_text`` and ``_extract_oc_pdf_text``
    on the same bytes would collide — whichever ran first would win the
    cache slot and the other would silently return the wrong result.
    """
    fn_id = real_fn.__qualname__

    def _cached(pdf_bytes: bytes) -> str:
        cache_dir = _pdf_cache_dir
        if cache_dir is None:
            # Fixture not yet initialised — fall back to uncached call
            return real_fn(pdf_bytes)

        key = hashlib.sha256(pdf_bytes).hexdigest()
        cache_file = cache_dir / f"{fn_id}_{key}.txt"

        # Fast path: cache hit
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")

        # Slow path: call real extractor, write to cache
        result = real_fn(pdf_bytes)

        # Atomic-ish write: write to a PID-unique temp file then rename.
        # Using os.getpid() ensures each xdist worker writes to its own
        # temp file, avoiding a race where two workers clobber the same
        # .tmp path.  On POSIX, os.rename atomically replaces the target
        # so the last writer wins — both produce identical content.
        tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(result, encoding="utf-8")
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
    """Wrap PDF text extraction with a cross-worker disk cache.

    Uses ``unittest.mock.patch`` instead of ``monkeypatch`` because
    ``monkeypatch`` is limited to function scope.  Every module that
    defines *or imports* ``_extract_pdf_text`` / ``extract_pdf_text``
    must be patched so that the imported reference in each module's
    namespace is replaced.
    """
    # --- pdf_link_scraper._extract_pdf_text (OC, Riverside, SB, Fresno, CC) ---
    _cached_pls = _make_disk_cached(_pls._extract_pdf_text)

    # --- sc_tentatives.extract_pdf_text (Santa Clara) ---
    _cached_sc = _make_disk_cached(_sc.extract_pdf_text)

    # --- ventura_tentatives._extract_pdf_text (local copy) ---
    _cached_ven = _make_disk_cached(_ven._extract_pdf_text)

    # --- oc_tentatives._extract_oc_pdf_text (pdfplumber table detection) ---
    _cached_oc = _make_disk_cached(_oc._extract_oc_pdf_text)

    # Patch every module that holds its own reference to the function.
    patches = [
        patch.object(_pls, "_extract_pdf_text", _cached_pls),
        patch.object(_cc, "_extract_pdf_text", _cached_pls),
        patch.object(_riv, "_extract_pdf_text", _cached_pls),
        patch.object(_fresno, "_extract_pdf_text", _cached_pls),
        patch.object(_sc, "extract_pdf_text", _cached_sc),
        patch.object(_ven, "_extract_pdf_text", _cached_ven),
        patch.object(_oc, "_extract_oc_pdf_text", _cached_oc),
    ]

    # --- backfill_oc_ruling_text._extract_oc_pdf_text (inlined copy) ---
    # The backfill script has its own copy of the OC extraction logic.
    # Without this patch, tests in test_backfill_oc_ruling_text.py do
    # uncached pdfplumber parses (~5s each), wasting ~30s total.
    _backfill_mod = _import_backfill_oc()
    if _backfill_mod is not None:
        _cached_backfill_oc = _make_disk_cached(
            _backfill_mod._extract_oc_pdf_text,
        )
        patches.append(
            patch.object(_backfill_mod, "_extract_oc_pdf_text", _cached_backfill_oc),
        )

    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


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
