"""Tests for framework.proxy_health — 407 proxy-auth classification (#4638)."""

from __future__ import annotations

import urllib.error
from typing import Any

from structlog.testing import capture_logs

from framework.proxy_health import (
    EgressProbeResult,
    ProxyAuthStatus,
    classify_proxy_error,
    diagnose_and_log_proxy_auth,
    probe_http_egress,
)


class _FakeResponse:
    """Minimal stand-in for a urllib response object."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, *, exc: Exception | None = None, response: Any = None) -> None:
        self._exc = exc
        self._response = response

    def open(self, request: Any, timeout: float | None = None) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._response


# ---------------------------------------------------------------------------
# classify_proxy_error
# ---------------------------------------------------------------------------


class TestClassifyProxyError:
    def test_tunnel_407_is_auth_failed(self) -> None:
        assert (
            classify_proxy_error("Tunnel connection failed: 407 Auth failed")
            is ProxyAuthStatus.AUTH_FAILED
        )

    def test_operation_timed_out_is_unreachable(self) -> None:
        assert (
            classify_proxy_error("<urlopen error [Errno 60] Operation timed out>")
            is ProxyAuthStatus.UNREACHABLE
        )

    def test_chromium_masked_timeout_is_unreachable(self) -> None:
        # The whole reason we do a secondary stdlib probe: the raw Chromium
        # string is NOT a 407.
        assert classify_proxy_error("net::ERR_TIMED_OUT") is ProxyAuthStatus.UNREACHABLE

    def test_proxy_authentication_required_is_auth_failed(self) -> None:
        assert (
            classify_proxy_error("407 Proxy Authentication Required") is ProxyAuthStatus.AUTH_FAILED
        )

    def test_empty_string_is_unreachable(self) -> None:
        assert classify_proxy_error("") is ProxyAuthStatus.UNREACHABLE


# ---------------------------------------------------------------------------
# probe_http_egress
# ---------------------------------------------------------------------------


class TestProbeHttpEgress:
    def test_tunnel_407_url_error_is_auth_failed(self) -> None:
        opener = _FakeOpener(exc=urllib.error.URLError("Tunnel connection failed: 407 Auth failed"))
        result = probe_http_egress(
            "https://api.ipify.org",
            proxy_url="http://user:pass@proxy:1234",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.AUTH_FAILED
        assert result.http_status == 407
        assert result.latency_s is not None

    def test_timeout_url_error_is_unreachable(self) -> None:
        opener = _FakeOpener(
            exc=urllib.error.URLError("<urlopen error [Errno 60] Operation timed out>")
        )
        result = probe_http_egress(
            "https://api.ipify.org",
            proxy_url="http://user:pass@proxy:1234",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.UNREACHABLE
        assert result.http_status is None

    def test_200_response_is_healthy_with_body(self) -> None:
        opener = _FakeOpener(response=_FakeResponse(b"44.224.204.57\n", status=200))
        result = probe_http_egress(
            "https://api.ipify.org",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.HEALTHY
        assert result.http_status == 200
        assert result.body == "44.224.204.57"

    def test_http_error_407_is_auth_failed(self) -> None:
        http_error = urllib.error.HTTPError(
            url="http://api.ipify.org",
            code=407,
            msg="Proxy Authentication Required",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        opener = _FakeOpener(exc=http_error)
        result = probe_http_egress(
            "http://api.ipify.org",
            proxy_url="http://user:pass@proxy:1234",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.AUTH_FAILED
        assert result.http_status == 407

    def test_http_error_non_407_is_unreachable(self) -> None:
        http_error = urllib.error.HTTPError(
            url="http://api.ipify.org",
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        opener = _FakeOpener(exc=http_error)
        result = probe_http_egress(
            "http://api.ipify.org",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.UNREACHABLE
        assert result.http_status == 503

    def test_unexpected_error_is_unreachable(self) -> None:
        opener = _FakeOpener(exc=ConnectionResetError("boom"))
        result = probe_http_egress(
            "https://api.ipify.org",
            opener_factory=lambda _proxy: opener,
        )
        assert result.status is ProxyAuthStatus.UNREACHABLE


# ---------------------------------------------------------------------------
# diagnose_and_log_proxy_auth
# ---------------------------------------------------------------------------


def _auth_failed_probe(*_args: Any, **_kwargs: Any) -> EgressProbeResult:
    return EgressProbeResult(
        status=ProxyAuthStatus.AUTH_FAILED,
        http_status=407,
        latency_s=1.29,
        detail="Tunnel connection failed: 407 Auth failed",
    )


def _unreachable_probe(*_args: Any, **_kwargs: Any) -> EgressProbeResult:
    return EgressProbeResult(
        status=ProxyAuthStatus.UNREACHABLE,
        http_status=None,
        latency_s=15.0,
        detail="timed out",
    )


class TestDiagnoseAndLogProxyAuth:
    def test_auth_failed_emits_distinct_event(self) -> None:
        import structlog

        log = structlog.get_logger("test")
        with capture_logs() as logs:
            status = diagnose_and_log_proxy_auth(
                log,
                "http://user:pass@proxy:1234",
                probe=_auth_failed_probe,
            )
        assert status is ProxyAuthStatus.AUTH_FAILED
        auth_events = [e for e in logs if e.get("event") == "proxy.auth_failed"]
        assert len(auth_events) == 1
        assert auth_events[0]["proxy_status"] == 407
        assert auth_events[0]["log_level"] == "error"

    def test_unreachable_does_not_emit_auth_failed(self) -> None:
        import structlog

        log = structlog.get_logger("test")
        with capture_logs() as logs:
            status = diagnose_and_log_proxy_auth(
                log,
                "http://user:pass@proxy:1234",
                probe=_unreachable_probe,
            )
        assert status is ProxyAuthStatus.UNREACHABLE
        assert not [e for e in logs if e.get("event") == "proxy.auth_failed"]
        # A plain timeout still records an info-level auth_check.
        assert [e for e in logs if e.get("event") == "proxy.auth_check"]

    def test_no_proxy_returns_no_proxy_without_probing(self) -> None:
        import structlog

        calls: list[Any] = []

        def _tracking_probe(*args: Any, **kwargs: Any) -> EgressProbeResult:
            calls.append((args, kwargs))
            return _auth_failed_probe()

        log = structlog.get_logger("test")
        with capture_logs() as logs:
            status = diagnose_and_log_proxy_auth(log, None, probe=_tracking_probe)
        assert status is ProxyAuthStatus.NO_PROXY
        assert calls == []
        assert logs == []
