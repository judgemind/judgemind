"""Tests for the shared OpenSearch client helper (SigV4 + local-dev fallbacks).

All boto3/opensearchpy construction is mocked — no real network or AWS calls.
Covers the three auth branches (local-dev basic-auth, SigV4, no-auth), hosts
string-vs-list normalization, and region resolution precedence.  See #4040.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from framework import opensearch_client
from framework.opensearch_client import make_opensearch_client


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean OpenSearch/AWS env."""
    for var in (
        "OPENSEARCH_USERNAME",
        "OPENSEARCH_PASSWORD",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


def _patch_opensearch() -> Any:
    """Patch the OpenSearch class so we can inspect construction kwargs."""
    return patch.object(opensearch_client, "OpenSearch")


def _session_with_creds(creds: object | None) -> MagicMock:
    """Build a mock boto3.Session whose get_credentials returns ``creds``."""
    session = MagicMock()
    session.get_credentials.return_value = creds
    session.region_name = None
    return session


class TestBasicAuthFallback:
    """Local-dev basic-auth fallback takes priority over SigV4."""

    def test_uses_http_auth_when_both_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSEARCH_USERNAME", "admin")
        monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")

        with _patch_opensearch() as mock_os:
            make_opensearch_client("http://localhost:9200")

        kwargs = mock_os.call_args.kwargs
        assert kwargs["http_auth"] == ("admin", "secret")
        # Basic-auth path must NOT install the SigV4 connection class.
        assert "connection_class" not in kwargs

    def test_basic_auth_skipped_when_only_username_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENSEARCH_USERNAME", "admin")
        # No password -> not basic auth; should fall through to SigV4.
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(MagicMock())
            make_opensearch_client("http://localhost:9200")

        kwargs = mock_os.call_args.kwargs
        assert kwargs.get("http_auth") != ("admin", "")
        assert "connection_class" in kwargs

    def test_basic_auth_keeps_timeout_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSEARCH_USERNAME", "admin")
        monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")

        with _patch_opensearch() as mock_os:
            make_opensearch_client("http://localhost:9200")

        kwargs = mock_os.call_args.kwargs
        assert kwargs["timeout"] == 30
        assert kwargs["max_retries"] == 3
        assert kwargs["retry_on_timeout"] is True


class TestSigV4Path:
    """SigV4 is selected when no basic-auth env is set and creds resolve."""

    def test_uses_sigv4_signer_when_creds_present(self) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(MagicMock())
            make_opensearch_client("https://search.example.com")

        kwargs = mock_os.call_args.kwargs
        assert isinstance(kwargs["http_auth"], opensearch_client.AWSV4SignerAuth)
        assert kwargs["connection_class"] is opensearch_client.RequestsHttpConnection

    def test_sigv4_keeps_timeout_knobs(self) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(MagicMock())
            make_opensearch_client(
                "https://search.example.com",
                timeout=45,
                max_retries=5,
                retry_on_timeout=False,
            )

        kwargs = mock_os.call_args.kwargs
        assert kwargs["timeout"] == 45
        assert kwargs["max_retries"] == 5
        assert kwargs["retry_on_timeout"] is False


class TestNoAuthFallback:
    """No basic-auth env and no resolvable creds -> plain no-auth client."""

    def test_no_auth_when_no_creds(self) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(None)
            make_opensearch_client("http://localhost:9200")

        kwargs = mock_os.call_args.kwargs
        assert "http_auth" not in kwargs
        assert "connection_class" not in kwargs

    def test_no_auth_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch(),
        ):
            mock_session_cls.return_value = _session_with_creds(None)
            with caplog.at_level("WARNING"):
                make_opensearch_client("http://localhost:9200")

        assert any(
            "no-auth" in rec.message.lower() or "no aws credentials" in rec.message.lower()
            for rec in caplog.records
        )


class TestHostsNormalization:
    """hosts accepts a string or a list; always normalized to a list."""

    def test_string_host_normalized_to_list(self) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(None)
            make_opensearch_client("http://localhost:9200")

        kwargs = mock_os.call_args.kwargs
        assert kwargs["hosts"] == ["http://localhost:9200"]

    def test_list_host_preserved(self) -> None:
        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            _patch_opensearch() as mock_os,
        ):
            mock_session_cls.return_value = _session_with_creds(None)
            make_opensearch_client(["http://a:9200", "http://b:9200"])

        kwargs = mock_os.call_args.kwargs
        assert kwargs["hosts"] == ["http://a:9200", "http://b:9200"]


class TestRegionResolution:
    """Region precedence: arg > env > botocore session > default us-west-2."""

    def test_explicit_arg_wins(self) -> None:
        captured: dict[str, str] = {}

        def _fake_signer(credentials: object, region: str, service: str) -> MagicMock:
            captured["region"] = region
            return MagicMock()

        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            patch.object(opensearch_client, "AWSV4SignerAuth", side_effect=_fake_signer),
            _patch_opensearch(),
        ):
            session = _session_with_creds(MagicMock())
            session.region_name = "eu-central-1"
            mock_session_cls.return_value = session
            make_opensearch_client("https://x", region="ap-south-1")

        assert captured["region"] == "ap-south-1"

    def test_env_used_when_no_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-2")
        captured: dict[str, str] = {}

        def _fake_signer(credentials: object, region: str, service: str) -> MagicMock:
            captured["region"] = region
            return MagicMock()

        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            patch.object(opensearch_client, "AWSV4SignerAuth", side_effect=_fake_signer),
            _patch_opensearch(),
        ):
            mock_session_cls.return_value = _session_with_creds(MagicMock())
            make_opensearch_client("https://x")

        assert captured["region"] == "us-east-2"

    def test_session_region_used_when_no_arg_or_env(self) -> None:
        captured: dict[str, str] = {}

        def _fake_signer(credentials: object, region: str, service: str) -> MagicMock:
            captured["region"] = region
            return MagicMock()

        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            patch.object(opensearch_client, "AWSV4SignerAuth", side_effect=_fake_signer),
            _patch_opensearch(),
        ):
            session = _session_with_creds(MagicMock())
            session.region_name = "eu-central-1"
            mock_session_cls.return_value = session
            make_opensearch_client("https://x")

        assert captured["region"] == "eu-central-1"

    def test_default_region_when_nothing_set(self) -> None:
        captured: dict[str, str] = {}

        def _fake_signer(credentials: object, region: str, service: str) -> MagicMock:
            captured["region"] = region
            captured["service"] = service
            return MagicMock()

        with (
            patch.object(opensearch_client.boto3, "Session") as mock_session_cls,
            patch.object(opensearch_client, "AWSV4SignerAuth", side_effect=_fake_signer),
            _patch_opensearch(),
        ):
            mock_session_cls.return_value = _session_with_creds(MagicMock())
            make_opensearch_client("https://x")

        assert captured["region"] == "us-west-2"
        assert captured["service"] == "es"
