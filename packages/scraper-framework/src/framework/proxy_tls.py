"""Proxy-only trust of the Bright Data Root CA (#4668). Self-contained and removable.

The Bright Data residential zone (``brd.superproxy.io:44445``) intercepts TLS:
every proxied HTTPS response is re-signed by ``CN=Bright Data Intermediate CA
2026``, which chains to ``CN=Bright Data Root CA``. Neither Chromium nor
Python trust that root, so every proxied request fails certificate
verification.

The operator decision on #4668 is to trust that root **narrowly**:

* only for traffic that goes through the proxy (every helper here is a no-op
  when no proxy URL is configured);
* never in the system / OS trust store;
* never by turning verification off (no ``verify=False``, no Playwright
  ``ignore_https_errors``). Verification stays fully on; the Bright Data root
  is simply one extra trust anchor for proxied connections.

Two consumers:

**Python HTTP clients** (urllib / httpx / requests) use
:func:`proxied_ssl_context`: certifi's default roots plus the Bright Data root.

**Chromium (Playwright / Patchright)** uses :func:`chromium_proxy_tls_launch_kwargs`,
which builds a private NSS database holding only the Bright Data root and
points the proxied browser process at it by overriding ``HOME`` for that
browser only (Chromium on Linux reads locally-trusted anchors from
``$HOME/.pki/nssdb``). The alternative the issue suggested,
``--ignore-certificate-errors-spki-list=<root SPKI>``, was tested against the
live proxy and does NOT work: Chromium only matches that list against the
certificates the server *presents*, and the proxy presents leaf + intermediate
but not the root, so a root pin never matches (``ERR_CERT_AUTHORITY_INVALID``).
Pinning the intermediate instead would break when it rotates (2027-08-03). A
per-browser NSS DB anchors on the root and survives intermediate rotation.

Removal: when Bright Data KYC lands and the zone stops intercepting TLS,
delete this module, ``framework/certs/brightdata_root_ca.crt``, the
``libnss3-tools`` line in the scraper Dockerfile, and the call sites
(grep ``proxy_tls``).
"""

from __future__ import annotations

import atexit
import os
import shutil
import ssl
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import certifi
import structlog

logger = structlog.get_logger(__name__)

#: Bright Data Root CA (official download, see header of the file for provenance).
BRIGHTDATA_ROOT_CA_PATH = Path(__file__).parent / "certs" / "brightdata_root_ca.crt"

#: SHA-256 fingerprint of the committed root certificate (DER), colon-free upper hex.
BRIGHTDATA_ROOT_CA_SHA256 = "DB8548F8A5B1166536920CCD0473840F7FDBAF165DEDF907B7B52361ABC87B60"

#: NSS nickname used for the root inside the private per-browser NSS DB.
NSSDB_CERT_NICKNAME = "Bright Data Root CA (judgemind proxy-only)"

# Per-process cache of the private HOME containing the NSS DB. Built once,
# reused by every proxied browser launch, removed at interpreter exit.
_nss_home: str | None = None


def proxied_ssl_context(proxy_url: str | None) -> ssl.SSLContext | None:
    """Return a verifying SSL context for requests routed through ``proxy_url``.

    The context trusts certifi's default roots **plus** the Bright Data root.
    Hostname checking and ``CERT_REQUIRED`` stay on, so a certificate that does
    not chain to either set is still rejected.

    Returns ``None`` when no proxy is configured: callers keep their default
    (unchanged) TLS behaviour for non-proxied traffic.
    """
    if not proxy_url:
        return None
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(BRIGHTDATA_ROOT_CA_PATH))
    return ctx


def _build_nss_home(
    certutil: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    mkdtemp: Callable[..., str] = tempfile.mkdtemp,
) -> str:
    """Create a private HOME whose ``.pki/nssdb`` trusts only the Bright Data root."""
    home = mkdtemp(prefix="judgemind-proxy-tls-")
    try:
        nssdb = Path(home) / ".pki" / "nssdb"
        nssdb.mkdir(parents=True, exist_ok=True)
        db = f"sql:{nssdb}"
        runner([certutil, "-N", "-d", db, "--empty-password"], check=True, capture_output=True)
        runner(
            [
                certutil,
                "-A",
                "-d",
                db,
                "-n",
                NSSDB_CERT_NICKNAME,
                "-t",
                "C,,",  # trusted CA for TLS server auth only (no email / code signing)
                "-a",
                "-i",
                str(BRIGHTDATA_ROOT_CA_PATH),
            ],
            check=True,
            capture_output=True,
        )
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return home


def _cleanup_nss_home() -> None:
    global _nss_home
    if _nss_home:
        shutil.rmtree(_nss_home, ignore_errors=True)
        _nss_home = None


def chromium_proxy_tls_launch_kwargs(
    proxy_url: str | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
    mkdtemp: Callable[..., str] = tempfile.mkdtemp,
    base_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Extra ``browser_type.launch()`` kwargs so a *proxied* Chromium trusts the BD root.

    Returns ``{}`` when no proxy is configured (non-proxied launches are
    untouched). Otherwise returns ``{"env": {...}}`` — the current environment
    with ``HOME`` pointed at a private directory whose NSS DB contains only the
    Bright Data root (trust ``C,,``). Only this browser process sees that HOME.

    If ``certutil`` (Debian ``libnss3-tools``) is unavailable — e.g. local macOS
    dev, where Chromium uses the Keychain rather than NSS — logs
    ``proxy_tls.nssdb_unavailable`` and returns ``{}``; the proxied navigation
    then fails verification loudly (``ERR_CERT_AUTHORITY_INVALID``) rather than
    silently skipping it.
    """
    global _nss_home
    if not proxy_url:
        return {}
    if _nss_home is None or not Path(_nss_home, ".pki", "nssdb").is_dir():
        certutil = which("certutil")
        if not certutil:
            logger.warning(
                "proxy_tls.nssdb_unavailable",
                reason="certutil not found (install libnss3-tools)",
            )
            return {}
        try:
            _nss_home = _build_nss_home(certutil, runner=runner, mkdtemp=mkdtemp)
        except (OSError, subprocess.CalledProcessError) as exc:
            logger.warning("proxy_tls.nssdb_unavailable", reason=str(exc))
            return {}
        atexit.register(_cleanup_nss_home)
        logger.info("proxy_tls.nssdb_ready", nickname=NSSDB_CERT_NICKNAME)
    env = dict(os.environ if base_env is None else base_env)
    env["HOME"] = _nss_home
    return {"env": env}
