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
import threading
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telemetry_upload


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
        k: sys.modules.get(k)
        for k in ("boto3", "botocore", "botocore.exceptions", "botocore.config")
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
        sys.modules["botocore.config"] = MagicMock()

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
        sys.modules["botocore.config"] = MagicMock()

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
        sys.modules["botocore.config"] = MagicMock()

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
        sys.modules["botocore.config"] = MagicMock()

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
        sys.modules["botocore.config"] = MagicMock()

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
        sys.modules["botocore.config"] = MagicMock()

        telemetry_upload.mirror_to_s3(
            local_file,
            "ralph-reviews/2026-04-26/agent-deadbeef-999.jsonl",
        )

        call_kwargs = mock_s3_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "judgemind-document-archive-dev"

    def test_client_gets_bounded_timeouts(self, tmp_path: Path) -> None:
        """The boto3 client is built with connect/read timeouts and retries (#5116)."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"x\n")
        mock_boto3 = _install_fake_boto3()
        mock_config_mod = sys.modules["botocore.config"]

        assert telemetry_upload.mirror_to_s3(local_file, "k", bucket="b") is True

        config_kwargs = mock_config_mod.Config.call_args[1]  # type: ignore[union-attr]
        assert (
            config_kwargs["connect_timeout"] == telemetry_upload._CONNECT_TIMEOUT_SECS
        )
        assert config_kwargs["read_timeout"] == telemetry_upload._READ_TIMEOUT_SECS
        assert (
            config_kwargs["retries"]["max_attempts"] == telemetry_upload._MAX_ATTEMPTS
        )
        assert mock_boto3.client.call_args[1]["config"] is (
            mock_config_mod.Config.return_value  # type: ignore[union-attr]
        )

    def test_unexpected_exception_returns_false(self, tmp_path: Path) -> None:
        """A botocore timeout (not a ClientError) is swallowed, not raised (#5116)."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"x\n")
        mock_boto3 = _install_fake_boto3()
        mock_boto3.client.return_value.put_object.side_effect = TimeoutError("read")

        assert telemetry_upload.mirror_to_s3(local_file, "k", bucket="b") is False


def _install_fake_boto3() -> MagicMock:
    """Install mock boto3/botocore modules; return the boto3 mock."""
    mock_boto3 = MagicMock()
    sys.modules["boto3"] = mock_boto3
    sys.modules["botocore"] = MagicMock()
    sys.modules["botocore.exceptions"] = _make_botocore_exceptions()
    sys.modules["botocore.config"] = MagicMock()
    return mock_boto3


class TestMirrorWithDeadline:
    """Overall deadline and CLI outcome reporting (#5116)."""

    def test_hung_upload_fails_at_deadline(self, tmp_path: Path) -> None:
        """A put_object that never returns is cut off at the deadline."""
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"x\n")
        release = threading.Event()
        mock_boto3 = _install_fake_boto3()
        mock_boto3.client.return_value.put_object.side_effect = lambda **_: (
            release.wait(30)
        )

        start = time.monotonic()
        ok, message = telemetry_upload.mirror_with_deadline(
            local_file, "k", 0.2, bucket="b"
        )
        elapsed = time.monotonic() - start
        release.set()

        assert ok is False
        assert "did not finish within 0.2s" in message
        assert elapsed < 5

    def test_success_within_deadline(self, tmp_path: Path) -> None:
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"abc")
        _install_fake_boto3()

        ok, message = telemetry_upload.mirror_with_deadline(
            local_file, "ralph-reviews/x.jsonl", 5, bucket="b"
        )

        assert ok is True
        assert message == (
            f"uploaded {local_file} to s3://b/ralph-reviews/x.jsonl (3 bytes)"
        )

    def test_main_prints_ok_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"abc")
        _install_fake_boto3()

        code = telemetry_upload.main([str(local_file), "k"])

        assert code == 0
        assert capsys.readouterr().out.startswith("ok: uploaded ")

    def test_main_prints_failure_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        local_file = tmp_path / "review-log.jsonl"
        local_file.write_bytes(b"abc")
        sys.modules["boto3"] = None  # type: ignore[assignment]

        code = telemetry_upload.main([str(local_file), "k"])

        assert code == 1
        assert capsys.readouterr().out.startswith("failed: boto3 not available")

    def test_main_reports_unreadable_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _install_fake_boto3()

        code = telemetry_upload.main([str(tmp_path / "missing.jsonl"), "k"])

        assert code == 1
        assert "failed: cannot read" in capsys.readouterr().out

    def test_main_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert telemetry_upload.main(["only-one"]) == 2
        assert "Usage:" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", telemetry_upload._DEFAULT_DEADLINE_SECS),
            ("abc", telemetry_upload._DEFAULT_DEADLINE_SECS),
            ("0", telemetry_upload._DEFAULT_DEADLINE_SECS),
            ("3.5", 3.5),
        ],
    )
    def test_deadline_from_env(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
    ) -> None:
        monkeypatch.setenv("JUDGEMIND_TELEMETRY_DEADLINE_SECS", raw)
        assert telemetry_upload._deadline_from_env() == expected
