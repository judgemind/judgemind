"""Tests for telemetry_upload module.

Covers:
- success returns True
- ClientError from put_object returns False without raising
- NoCredentialsError returns False without raising
- ImportError (boto3 absent) returns False without raising
- correct Bucket and Key are passed to put_object
- bucket defaults to JUDGEMIND_TELEMETRY_BUCKET env var
- bucket falls back to judgemind-document-archive-dev when env var absent

telemetry_upload lazy-imports boto3 inside the function body, so we control
boto3/botocore availability by manipulating sys.modules before each call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telemetry_upload  # noqa: E402


def _make_botocore_exceptions(
    client_error: type[Exception] | None = None,
    endpoint_error: type[Exception] | None = None,
    no_creds_error: type[Exception] | None = None,
) -> MagicMock:
    """Build a mock botocore.exceptions module with configurable exception types."""
    mock_exc = MagicMock()
    mock_exc.ClientError = client_error or Exception
    mock_exc.EndpointConnectionError = endpoint_error or Exception
    mock_exc.NoCredentialsError = no_creds_error or Exception
    return mock_exc


@pytest.fixture(autouse=True)
def _restore_boto3_modules() -> Generator[None, None, None]:
    """Ensure boto3/botocore are restored in sys.modules after each test."""
    saved = {
        k: sys.modules.get(k) for k in ("boto3", "botocore", "botocore.exceptions")
    }
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class TestMirrorToS3:
    """Tests for mirror_to_s3 function."""

    def test_success_returns_true(self, tmp_path: Path) -> None:
        """Successful put_object call returns True."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b'{"type": "summary"}\n')

        mock_s3_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions()

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        result = telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-abc-123.jsonl",
            bucket="test-bucket",
        )

        assert result is True
        mock_s3_client.put_object.assert_called_once()

    def test_client_error_returns_false(self, tmp_path: Path) -> None:
        """ClientError from put_object returns False without raising."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b'{"type": "summary"}\n')

        class FakeClientError(Exception):
            pass

        mock_s3_client = MagicMock()
        mock_s3_client.put_object.side_effect = FakeClientError("Access Denied")
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions(client_error=FakeClientError)

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        result = telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-abc-123.jsonl",
            bucket="test-bucket",
        )

        assert result is False

    def test_no_credentials_error_returns_false(self, tmp_path: Path) -> None:
        """NoCredentialsError returns False without raising."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b'{"type": "summary"}\n')

        class FakeNoCredentialsError(Exception):
            pass

        mock_s3_client = MagicMock()
        mock_s3_client.put_object.side_effect = FakeNoCredentialsError("No creds")
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions(
            no_creds_error=FakeNoCredentialsError
        )

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        result = telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-abc-123.jsonl",
            bucket="test-bucket",
        )

        assert result is False

    def test_import_error_swallowed_when_boto3_absent(self, tmp_path: Path) -> None:
        """ImportError when boto3 is absent returns False without raising."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b'{"type": "summary"}\n')

        # Simulate boto3 being completely absent
        sys.modules["boto3"] = None  # type: ignore[assignment]

        result = telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-abc-123.jsonl",
            bucket="test-bucket",
        )

        assert result is False

    def test_correct_bucket_and_key_passed(self, tmp_path: Path) -> None:
        """Correct Bucket and Key are forwarded to put_object."""
        local_file = tmp_path / "review-log.jsonl"
        content = b'{"type": "review"}\n{"type": "summary"}\n'
        local_file.write_bytes(content)

        mock_s3_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions()

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-deadbeef-999.jsonl",
            bucket="judgemind-document-archive-dev",
        )

        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "judgemind-document-archive-dev"
        assert call_kwargs["Key"] == "ralph-reviews/2026-04-26/agent-deadbeef-999.jsonl"
        assert call_kwargs["Body"] == content

    def test_default_bucket_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When bucket=None, uses JUDGEMIND_TELEMETRY_BUCKET env var."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"test\n")
        monkeypatch.setenv("JUDGEMIND_TELEMETRY_BUCKET", "my-custom-bucket")

        mock_s3_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions()

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-deadbeef-999.jsonl",
        )

        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-custom-bucket"

    def test_default_bucket_fallback_when_env_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to judgemind-document-archive-dev when env var not set."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"test\n")
        monkeypatch.delenv("JUDGEMIND_TELEMETRY_BUCKET", raising=False)

        mock_s3_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3_client
        mock_botocore_exc = _make_botocore_exceptions()

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = MagicMock()
        sys.modules["botocore.exceptions"] = mock_botocore_exc

        telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-deadbeef-999.jsonl",
        )

        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "judgemind-document-archive-dev"
