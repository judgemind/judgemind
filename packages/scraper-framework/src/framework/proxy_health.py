"""Proxy / anti-bot infra health classification (#4633 / #4638).

Shared, stdlib-only home for HTTP ``407`` proxy-auth-failure classification.
Reused by both the anti-bot scrapers (``sd_tentatives``, ``sf_civil_tentatives``)
and the operator probe script (``scripts/probe_antibot_proxy.py``) so there is a
single tested definition of "is this a proxy credential rejection or a plain
timeout?".

Background (#4633): a lapsed Bright Data residential proxy credential took down
the SD + SF anti-bot scrapers, but the failure surfaced only as an opaque
Chromium ``net::ERR_TIMED_OUT``. Chromium masks the repeated ``407`` CONNECT
challenge as a timeout; urllib surfaces the raw ``Tunnel connection failed: 407
Auth failed``. Because Bright Data authenticates by customer/zone user:pass
embedded in the proxy URL (not by source IP), a ``407`` is location-independent
— it fails identically from a laptop and from the scraper VPC — so a secondary
stdlib probe through the SAME proxy against a neutral non-court target
distinguishes a *proxy auth failure* from a *court block* or a genuine timeout.

Only the stdlib is used for the network legs (``urllib``) so this module stays
dependency-light and runnable anywhere.
"""

from __future__ import annotations

import enum
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Short, honest User-Agent so the request is not obviously a bare urllib client.
_USER_AGENT = "judgemind-proxy-health/1.0"

# Substrings in a proxy error string that indicate an HTTP 407 credential
# rejection. A plain timeout ('timed out', Chromium 'net::ERR_TIMED_OUT') and a
# connection refusal carry none of these, so they classify as UNREACHABLE.
_AUTH_MARKERS = ("407", "auth failed", "proxy authentication required")


class ProxyAuthStatus(enum.Enum):
    """Outcome of a proxy egress probe."""

    HEALTHY = "healthy"
    AUTH_FAILED = "auth_failed"  # HTTP 407 — proxy credential rejected
    UNREACHABLE = "unreachable"  # timeout / conn refused / DNS — NOT an auth failure
    NO_PROXY = "no_proxy"  # no proxy_url configured


@dataclass
class EgressProbeResult:
    """Result of a single :func:`probe_http_egress` call."""

    status: ProxyAuthStatus
    http_status: int | None
    latency_s: float | None
    detail: str
    body: str | None = None  # response body (the egress IP, for api.ipify.org)


def classify_proxy_error(error_text: str) -> ProxyAuthStatus:
    """Classify a proxy error string.

    Returns :attr:`ProxyAuthStatus.AUTH_FAILED` when the text carries a 407
    marker (``407``, ``auth failed``, ``proxy authentication required``);
    otherwise :attr:`ProxyAuthStatus.UNREACHABLE`.

    This is the crux of the module: it maps urllib's raw ``Tunnel connection
    failed: 407 Auth failed`` to ``AUTH_FAILED`` while a plain ``timed out`` /
    Chromium ``net::ERR_TIMED_OUT`` stays ``UNREACHABLE``.
    """
    lowered = (error_text or "").lower()
    for marker in _AUTH_MARKERS:
        if marker in lowered:
            return ProxyAuthStatus.AUTH_FAILED
    return ProxyAuthStatus.UNREACHABLE


def _default_opener_factory(proxy_url: str | None) -> urllib.request.OpenerDirector:
    """Build a real urllib opener, routing through ``proxy_url`` when set."""
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def probe_http_egress(
    target_url: str,
    *,
    proxy_url: str | None = None,
    timeout: float = 15.0,
    opener_factory: Callable[[str | None], Any] | None = None,
) -> EgressProbeResult:
    """Perform a single timed GET to ``target_url``, optionally through a proxy.

    * ``proxy_url`` ``None`` -> direct request (control leg).
    * ``proxy_url`` set -> route through a urllib ``ProxyHandler``.

    A ``407`` surfaces as an :class:`urllib.error.HTTPError` with ``code == 407``
    for an HTTP target, or (for an HTTPS CONNECT tunnel) as an
    :class:`urllib.error.URLError` whose reason contains ``407 Auth failed``.
    Both are mapped to :attr:`ProxyAuthStatus.AUTH_FAILED`. A ``2xx`` response is
    ``HEALTHY`` with the body captured (the egress IP, for ``api.ipify.org``).

    ``opener_factory`` is an injection seam for tests; the default builds a real
    urllib opener with the proxy handler.
    """
    factory = opener_factory or _default_opener_factory
    request = urllib.request.Request(target_url, headers={"User-Agent": _USER_AGENT})
    start = time.monotonic()
    try:
        opener = factory(proxy_url)
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        latency = time.monotonic() - start
        # HTTPError is a file-like response object; close it to avoid leaking
        # the underlying connection / file descriptor on repeated errors
        # (the 407 this probe detects, or any other server error). fp=None is a
        # no-op, so this is safe for synthesized errors in tests too.
        exc.close()
        if exc.code == 407:
            return EgressProbeResult(
                status=ProxyAuthStatus.AUTH_FAILED,
                http_status=407,
                latency_s=latency,
                detail=f"HTTP 407 proxy authentication required: {exc}",
            )
        return EgressProbeResult(
            status=ProxyAuthStatus.UNREACHABLE,
            http_status=exc.code,
            latency_s=latency,
            detail=f"HTTP {exc.code}: {exc}",
        )
    except urllib.error.URLError as exc:
        latency = time.monotonic() - start
        reason = str(exc.reason)
        status = classify_proxy_error(reason)
        return EgressProbeResult(
            status=status,
            http_status=407 if status is ProxyAuthStatus.AUTH_FAILED else None,
            latency_s=latency,
            detail=reason,
        )
    except Exception as exc:  # noqa: BLE001 — any transport error is "unreachable"
        latency = time.monotonic() - start
        return EgressProbeResult(
            status=ProxyAuthStatus.UNREACHABLE,
            http_status=None,
            latency_s=latency,
            detail=str(exc),
        )

    latency = time.monotonic() - start
    try:
        raw = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    body = raw.decode("utf-8", errors="replace").strip() if raw else ""
    getcode = getattr(response, "getcode", None)
    http_status = getattr(response, "status", None)
    if http_status is None and callable(getcode):
        http_status = getcode()
    return EgressProbeResult(
        status=ProxyAuthStatus.HEALTHY,
        http_status=http_status,
        latency_s=latency,
        detail="ok",
        body=body,
    )


def diagnose_and_log_proxy_auth(
    log: Any,
    proxy_url: str | None,
    *,
    neutral_url: str = "https://api.ipify.org",
    timeout: float = 15.0,
    probe: Callable[..., EgressProbeResult] = probe_http_egress,
) -> ProxyAuthStatus:
    """Diagnose an opaque proxied-navigation failure and log distinctly.

    After a proxied navigation fails opaquely (Chromium ``net::ERR_TIMED_OUT``),
    run a stdlib probe through the SAME proxy against a neutral non-court target.
    When the proxy rejects auth with ``407``, emit a distinct
    ``proxy.auth_failed`` structlog event so a Bright Data credential lapse
    surfaces explicitly instead of the opaque timeout. Returns the classified
    status.

    * ``proxy_url`` falsy -> return :attr:`ProxyAuthStatus.NO_PROXY`; do not
      probe, do not log.
    * ``AUTH_FAILED`` -> ``log.error("proxy.auth_failed", proxy_status=407, ...)``
      with a rotation hint.
    * otherwise -> ``log.info("proxy.auth_check", status=..., ...)``.

    ``probe`` is injectable for tests. ``log`` is a structlog BoundLogger (the
    scraper's ``self._log``).
    """
    if not proxy_url:
        return ProxyAuthStatus.NO_PROXY

    result = probe(neutral_url, proxy_url=proxy_url, timeout=timeout)

    if result.status is ProxyAuthStatus.AUTH_FAILED:
        log.error(
            "proxy.auth_failed",
            proxy_status=407,
            neutral_target=neutral_url,
            detail=result.detail,
            latency_s=result.latency_s,
            hint=(
                "proxy credential rejected (HTTP 407) — rotate SD_PROXY_URL / "
                "Bright Data zone password; not a court block or timeout"
            ),
        )
    else:
        log.info(
            "proxy.auth_check",
            status=result.status.value,
            neutral_target=neutral_url,
            detail=result.detail,
            latency_s=result.latency_s,
        )
    return result.status
