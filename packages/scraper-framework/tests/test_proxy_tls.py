"""Tests for framework.proxy_tls — proxy-only trust of the Bright Data root (#4668)."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from framework import proxy_tls
from framework.proxy_tls import (
    BRIGHTDATA_ROOT_CA_PATH,
    BRIGHTDATA_ROOT_CA_SHA256,
    NSSDB_CERT_NICKNAME,
    chromium_proxy_tls_launch_kwargs,
    proxied_ssl_context,
)

PROXY = "http://user:pass@brd.superproxy.io:44445"


@pytest.fixture(autouse=True)
def _reset_nss_home() -> Iterator[None]:
    proxy_tls._cleanup_nss_home()
    yield
    proxy_tls._cleanup_nss_home()


class TestCommittedRoot:
    def test_file_ships_inside_framework_package(self) -> None:
        assert BRIGHTDATA_ROOT_CA_PATH.is_file()
        assert BRIGHTDATA_ROOT_CA_PATH.parent.parent.name == "framework"

    def test_fingerprint_matches_recorded_constant(self) -> None:
        pem = BRIGHTDATA_ROOT_CA_PATH.read_text()
        start = pem.index("-----BEGIN CERTIFICATE-----")
        der = ssl.PEM_cert_to_DER_cert(pem[start:])
        assert hashlib.sha256(der).hexdigest().upper() == BRIGHTDATA_ROOT_CA_SHA256

    def test_exactly_one_certificate(self) -> None:
        assert BRIGHTDATA_ROOT_CA_PATH.read_text().count("BEGIN CERTIFICATE") == 1


class TestProxiedSslContext:
    def test_no_proxy_returns_none(self) -> None:
        """Non-proxied callers keep their default TLS behaviour."""
        assert proxied_ssl_context(None) is None
        assert proxied_ssl_context("") is None

    def test_verification_stays_on(self) -> None:
        ctx = proxied_ssl_context(PROXY)
        assert ctx is not None
        assert ctx.verify_mode is ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_trusts_certifi_plus_brightdata_root(self) -> None:
        ctx = proxied_ssl_context(PROXY)
        assert ctx is not None
        cas = ctx.get_ca_certs()
        subjects = [dict(item[0] for item in ca["subject"]) for ca in cas]
        assert {
            "countryName": "US",
            "organizationName": "Bright Data",
            "commonName": "Bright Data Root CA",
        } in subjects
        # certifi's public roots are present too (not a BD-only store).
        assert len(cas) > 50

    def test_rejects_self_signed_cert_not_chaining_to_brightdata_root(self, tmp_path: Path) -> None:
        """AC2: anything not chaining to certifi or the BD root is still rejected."""
        x509 = pytest.importorskip("cryptography.x509")
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256())
        )
        cert_file = tmp_path / "server.crt"
        key_file = tmp_path / "server.key"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_file, key_file)
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def _serve() -> None:
            conn, _ = listener.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        ctx = proxied_ssl_context(PROXY)
        assert ctx is not None
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with pytest.raises(ssl.SSLCertVerificationError):
                    ctx.wrap_socket(raw, server_hostname="localhost")
        finally:
            thread.join(timeout=5)
            listener.close()


class _Runner:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str], **kwargs: Any) -> None:
        self.calls.append(cmd)
        if self.fail:
            raise subprocess.CalledProcessError(255, cmd)


class TestChromiumLaunchKwargs:
    def test_no_proxy_is_noop(self) -> None:
        runner = _Runner()
        assert chromium_proxy_tls_launch_kwargs(None, runner=runner) == {}
        assert chromium_proxy_tls_launch_kwargs("", runner=runner) == {}
        assert runner.calls == []

    def test_builds_private_nssdb_and_overrides_home(self, tmp_path: Path) -> None:
        runner = _Runner()
        home = tmp_path / "home"

        def mkdtemp(**_: Any) -> str:
            home.mkdir()
            return str(home)

        kwargs = chromium_proxy_tls_launch_kwargs(
            PROXY,
            which=lambda _: "/usr/bin/certutil",
            runner=runner,
            mkdtemp=mkdtemp,
            base_env={"PATH": "/bin", "HOME": "/home/scraper"},
        )
        assert kwargs == {"env": {"PATH": "/bin", "HOME": str(home)}}
        nssdb = f"sql:{home / '.pki' / 'nssdb'}"
        assert runner.calls[0] == ["/usr/bin/certutil", "-N", "-d", nssdb, "--empty-password"]
        add = runner.calls[1]
        assert add[:3] == ["/usr/bin/certutil", "-A", "-d"]
        assert add[add.index("-t") + 1] == "C,,"
        assert add[add.index("-n") + 1] == NSSDB_CERT_NICKNAME
        assert add[-1] == str(BRIGHTDATA_ROOT_CA_PATH)

    def test_nssdb_built_once_per_process(self, tmp_path: Path) -> None:
        runner = _Runner()
        home = tmp_path / "home"

        def mkdtemp(**_: Any) -> str:
            home.mkdir()
            return str(home)

        for _ in range(3):
            chromium_proxy_tls_launch_kwargs(
                PROXY, which=lambda _: "certutil", runner=runner, mkdtemp=mkdtemp
            )
        assert len(runner.calls) == 2

    # The module logger is patched (rather than using structlog's capture_logs)
    # because other tests configure structlog with cache_logger_on_first_use,
    # which makes capture_logs order-dependent in a serial run.
    def test_missing_certutil_logs_and_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log = MagicMock()
        monkeypatch.setattr(proxy_tls, "logger", log)
        assert chromium_proxy_tls_launch_kwargs(PROXY, which=lambda _: None) == {}
        assert log.warning.call_args.args[0] == "proxy_tls.nssdb_unavailable"

    def test_certutil_failure_logs_and_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = MagicMock()
        monkeypatch.setattr(proxy_tls, "logger", log)
        home = tmp_path / "home"

        def mkdtemp(**_: Any) -> str:
            home.mkdir()
            return str(home)

        result = chromium_proxy_tls_launch_kwargs(
            PROXY,
            which=lambda _: "certutil",
            runner=_Runner(fail=True),
            mkdtemp=mkdtemp,
        )
        assert result == {}
        assert log.warning.call_args.args[0] == "proxy_tls.nssdb_unavailable"
        # The half-built private HOME is removed on failure.
        assert not home.exists()

    def test_cleanup_removes_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"

        def mkdtemp(**_: Any) -> str:
            home.mkdir()
            return str(home)

        chromium_proxy_tls_launch_kwargs(
            PROXY, which=lambda _: "certutil", runner=_Runner(), mkdtemp=mkdtemp
        )
        assert home.exists()
        proxy_tls._cleanup_nss_home()
        assert not home.exists()
        assert proxy_tls._nss_home is None

    @pytest.mark.skipif(shutil.which("certutil") is None, reason="needs NSS certutil")
    def test_real_certutil_trusts_root_for_tls_only(self) -> None:
        kwargs = chromium_proxy_tls_launch_kwargs(PROXY)
        home = kwargs["env"]["HOME"]
        out = subprocess.run(
            ["certutil", "-L", "-d", f"sql:{home}/.pki/nssdb"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        line = next(ln for ln in out.splitlines() if NSSDB_CERT_NICKNAME in ln)
        assert line.split()[-1] == "C,,"
