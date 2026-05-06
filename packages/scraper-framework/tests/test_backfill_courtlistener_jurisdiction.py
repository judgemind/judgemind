"""Regression tests for ``scripts/backfill_courtlistener_jurisdiction.py``.

Filed under #4247: the original backfill read ``cluster.court`` from the S3
envelope and reported ``0 rebucketed`` on every run because that field is
empty in every CourtListener API response we capture.  The fix prefers
``docket.court`` from the new envelope shape and falls back to a live
docket fetch for old envelopes.

Tests focus on ``_extract_court_id_from_envelope`` — the pure helper that
maps an S3-envelope dict to a resolved court short-id.  The S3 read and
the live CourtListener fallback are exercised via small fakes; the DB
path is intentionally out of scope for these unit tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure scraper-framework src is on path before importing the script.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "packages" / "scraper-framework" / "src"))


def _load_backfill_module() -> object:
    """Import scripts/backfill_courtlistener_jurisdiction.py without running main."""
    script_path = _REPO_ROOT / "scripts" / "backfill_courtlistener_jurisdiction.py"
    spec = importlib.util.spec_from_file_location(
        "backfill_courtlistener_jurisdiction", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_courtlistener_jurisdiction"] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill_module()


# ---------------------------------------------------------------------------
# _extract_court_id_from_envelope tests
# ---------------------------------------------------------------------------


class TestExtractCourtIdFromEnvelope:
    """Pure-function tests against the S3-envelope reader (#4247)."""

    def test_prefers_docket_court_over_cluster_court(self) -> None:
        """When the envelope has both, docket.court wins (the canonical source)."""
        envelope = {
            "cluster": {"id": 1, "court": "/api/rest/v4/courts/parent/"},
            "opinion": {"id": 2},
            "docket": {
                "id": 73251044,
                "court": "/api/rest/v4/courts/texapp14/",
            },
        }
        assert backfill._extract_court_id_from_envelope(envelope) == "texapp14"

    def test_uses_docket_court_when_cluster_court_empty(self) -> None:
        """The actual CourtListener API shape: cluster.court is empty.

        Without the #4247 fix the old code returned None on every doc and
        produced "0 rebucketed".  This test asserts the new behavior.
        """
        envelope = {
            "cluster": {"id": 1, "court": ""},
            "opinion": {"id": 2},
            "docket": {
                "id": 73251044,
                "court": "/api/rest/v4/courts/texapp14/",
            },
        }
        assert backfill._extract_court_id_from_envelope(envelope) == "texapp14"

    def test_falls_back_to_cluster_court_when_no_docket_envelope_key(self) -> None:
        """Backward-compat: old envelopes (pre-#4247) lack a 'docket' key."""
        envelope = {
            "cluster": {"id": 1, "court": "/api/rest/v4/courts/scotus/"},
            "opinion": {"id": 2},
        }
        assert backfill._extract_court_id_from_envelope(envelope) == "scotus"

    def test_returns_none_when_both_empty_and_no_client(self) -> None:
        """No docket envelope key, no cluster.court, no live client -> None."""
        envelope = {"cluster": {"id": 1, "court": ""}, "opinion": {"id": 2}}
        assert backfill._extract_court_id_from_envelope(envelope) is None

    def test_live_docket_fetch_via_docket_url(self) -> None:
        """For old envelopes, the optional CourtListener client live-fetches the docket."""
        envelope = {
            "cluster": {
                "id": 1,
                "court": "",
                "docket": "https://www.courtlistener.com/api/rest/v4/dockets/99/",
            },
            "opinion": {"id": 2},
        }
        cl_client = MagicMock()
        cl_client.fetch_docket.return_value = {
            "id": 99,
            "court": "/api/rest/v4/courts/texapp14/",
        }

        result = backfill._extract_court_id_from_envelope(envelope, cl_client=cl_client)

        assert result == "texapp14"
        cl_client.fetch_docket.assert_called_once_with(
            "https://www.courtlistener.com/api/rest/v4/dockets/99/"
        )

    def test_live_docket_fetch_via_docket_id(self) -> None:
        """When only cluster.docket_id is present, build the URL from API_BASE_URL."""
        envelope = {
            "cluster": {"id": 1, "court": "", "docket_id": 12345},
            "opinion": {"id": 2},
        }
        cl_client = MagicMock()
        cl_client.fetch_docket.return_value = {
            "id": 12345,
            "court": "/api/rest/v4/courts/ny/",
        }

        result = backfill._extract_court_id_from_envelope(envelope, cl_client=cl_client)

        assert result == "ny"
        # Verify the URL was constructed from API_BASE_URL + docket_id.
        called_url = cl_client.fetch_docket.call_args[0][0]
        assert called_url.endswith("/dockets/12345/")

    def test_live_docket_fetch_skipped_when_no_docket_reference(self) -> None:
        """If cluster has neither docket URL nor docket_id, return None without fetching."""
        envelope = {"cluster": {"id": 1, "court": ""}, "opinion": {"id": 2}}
        cl_client = MagicMock()

        result = backfill._extract_court_id_from_envelope(envelope, cl_client=cl_client)

        assert result is None
        cl_client.fetch_docket.assert_not_called()

    def test_live_docket_fetch_failure_returns_none(self) -> None:
        """A failing live docket fetch returns None instead of raising."""
        envelope = {
            "cluster": {"id": 1, "court": "", "docket_id": 99},
            "opinion": {"id": 2},
        }
        cl_client = MagicMock()
        cl_client.fetch_docket.side_effect = Exception("HTTP 500")

        result = backfill._extract_court_id_from_envelope(envelope, cl_client=cl_client)

        assert result is None


# ---------------------------------------------------------------------------
# _extract_court_id_from_s3 — thin wrapper over the envelope reader
# ---------------------------------------------------------------------------


class TestExtractCourtIdFromS3:
    """Verify the S3 wrapper delegates to the pure helper."""

    def _make_s3_client(self, envelope: dict) -> MagicMock:
        body = MagicMock()
        body.read.return_value = json.dumps(envelope).encode("utf-8")
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body}
        return s3

    def test_reads_docket_court_from_s3_envelope(self) -> None:
        """End-to-end S3 read: envelope with docket.court resolves correctly."""
        envelope = {
            "cluster": {"id": 1, "court": ""},
            "opinion": {"id": 2},
            "docket": {
                "id": 73251044,
                "court": "/api/rest/v4/courts/texapp14/",
            },
        }
        s3 = self._make_s3_client(envelope)
        result = backfill._extract_court_id_from_s3(s3, "bucket", "key")
        assert result == "texapp14"

    def test_returns_none_when_s3_read_fails(self) -> None:
        s3 = MagicMock()
        s3.get_object.side_effect = Exception("NoSuchKey")
        result = backfill._extract_court_id_from_s3(s3, "bucket", "missing-key")
        assert result is None
