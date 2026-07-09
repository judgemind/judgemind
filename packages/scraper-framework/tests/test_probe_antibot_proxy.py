"""Tests for scripts/probe_antibot_proxy.py (#4638).

The script is imported via importlib after inserting scripts/ on sys.path, the
same pattern as test_cc_dual_run_diff_script.py.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from framework.proxy_health import EgressProbeResult, ProxyAuthStatus
from framework.turnstile_solver import CapsolverBalance

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

probe = import_module("probe_antibot_proxy")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _healthy_egress(
    target_url: str, *, proxy_url: str | None = None, **_kwargs: Any
) -> EgressProbeResult:
    return EgressProbeResult(
        status=ProxyAuthStatus.HEALTHY,
        http_status=200,
        latency_s=0.4,
        detail="ok",
        body="44.224.204.57" if proxy_url is None else None,
    )


def _auth_failed_egress(
    target_url: str, *, proxy_url: str | None = None, **_kwargs: Any
) -> EgressProbeResult:
    if proxy_url is None:
        # The direct control leg is healthy — egress itself is fine.
        return EgressProbeResult(
            status=ProxyAuthStatus.HEALTHY,
            http_status=200,
            latency_s=0.4,
            detail="ok",
            body="44.224.204.57",
        )
    return EgressProbeResult(
        status=ProxyAuthStatus.AUTH_FAILED,
        http_status=407,
        latency_s=1.29,
        detail="Tunnel connection failed: 407 Auth failed",
    )


async def _valid_balance(_key: Any) -> CapsolverBalance:
    return CapsolverBalance(valid=True, balance=12.5, error_code=None, error_description=None)


async def _invalid_balance(_key: Any) -> CapsolverBalance:
    return CapsolverBalance(
        valid=False,
        balance=None,
        error_code="ERROR_KEY_DOES_NOT_EXIST",
        error_description="clientKey is invalid",
    )


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------


class TestRunChecks:
    def test_auth_failed_proxy_rows_report_auth_failed(self) -> None:
        results = probe.run_checks(
            "http://user:pass@proxy:1234",
            "bad-key",
            egress_probe=_auth_failed_egress,
            balance_fn=_invalid_balance,
        )
        by_name = {r.name: r for r in results}
        assert by_name[probe.DIRECT_EGRESS].status == ProxyAuthStatus.HEALTHY.value
        assert by_name[probe.PROXIED_NEUTRAL].status == ProxyAuthStatus.AUTH_FAILED.value
        assert by_name[probe.PROXIED_SD_PORTAL].status == ProxyAuthStatus.AUTH_FAILED.value
        assert by_name[probe.PROXIED_SF_CAPTCHA].status == ProxyAuthStatus.AUTH_FAILED.value
        assert by_name[probe.CAPSOLVER_BALANCE].status == "invalid"
        assert "ERROR_KEY_DOES_NOT_EXIST" in by_name[probe.CAPSOLVER_BALANCE].detail

    def test_all_healthy_and_valid(self) -> None:
        results = probe.run_checks(
            "http://user:pass@proxy:1234",
            "good-key",
            egress_probe=_healthy_egress,
            balance_fn=_valid_balance,
        )
        by_name = {r.name: r for r in results}
        assert by_name[probe.PROXIED_NEUTRAL].status == ProxyAuthStatus.HEALTHY.value
        assert by_name[probe.CAPSOLVER_BALANCE].status == "valid"

    def test_no_proxy_marks_proxied_rows_no_proxy(self) -> None:
        results = probe.run_checks(
            None,
            "good-key",
            egress_probe=_healthy_egress,
            balance_fn=_valid_balance,
        )
        by_name = {r.name: r for r in results}
        assert by_name[probe.DIRECT_EGRESS].status == ProxyAuthStatus.HEALTHY.value
        assert by_name[probe.PROXIED_NEUTRAL].status == ProxyAuthStatus.NO_PROXY.value


# ---------------------------------------------------------------------------
# format_table
# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_renders_header_and_one_row_per_check(self) -> None:
        results = probe.run_checks(
            "http://user:pass@proxy:1234",
            "good-key",
            egress_probe=_healthy_egress,
            balance_fn=_valid_balance,
        )
        table = probe.format_table(results)
        lines = table.splitlines()
        # Header + separator + 5 checks.
        assert lines[0].split()[0] == "CHECK"
        assert "STATUS" in lines[0]
        assert len(lines) == 2 + len(results)
        for r in results:
            assert r.name in table
        assert "healthy" in table

    def test_auth_failed_status_appears(self) -> None:
        results = probe.run_checks(
            "http://user:pass@proxy:1234",
            "bad-key",
            egress_probe=_auth_failed_egress,
            balance_fn=_invalid_balance,
        )
        table = probe.format_table(results)
        assert ProxyAuthStatus.AUTH_FAILED.value in table


# ---------------------------------------------------------------------------
# main — exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_exit_1_on_auth_failed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SD_PROXY_URL", "http://user:pass@proxy:1234")
        monkeypatch.setenv("CAPSOLVER_API_KEY", "bad-key")
        monkeypatch.setattr(probe, "probe_http_egress", _auth_failed_egress)
        monkeypatch.setattr(probe, "get_balance", _invalid_balance)
        code = probe.main([])
        assert code == 1
        out = capsys.readouterr().out
        assert ProxyAuthStatus.AUTH_FAILED.value in out

    def test_exit_0_on_all_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SD_PROXY_URL", "http://user:pass@proxy:1234")
        monkeypatch.setenv("CAPSOLVER_API_KEY", "good-key")
        monkeypatch.setattr(probe, "probe_http_egress", _healthy_egress)
        monkeypatch.setattr(probe, "get_balance", _valid_balance)
        code = probe.main([])
        assert code == 0

    def test_json_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("SD_PROXY_URL", "http://user:pass@proxy:1234")
        monkeypatch.setenv("CAPSOLVER_API_KEY", "good-key")
        monkeypatch.setattr(probe, "probe_http_egress", _healthy_egress)
        monkeypatch.setattr(probe, "get_balance", _valid_balance)
        code = probe.main(["--json"])
        assert code == 0
        import json as _json

        payload = _json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert {r["name"] for r in payload} == {
            probe.DIRECT_EGRESS,
            probe.PROXIED_NEUTRAL,
            probe.PROXIED_SD_PORTAL,
            probe.PROXIED_SF_CAPTCHA,
            probe.CAPSOLVER_BALANCE,
        }

    def test_exit_1_when_proxy_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SD_PROXY_URL", raising=False)
        monkeypatch.setenv("CAPSOLVER_API_KEY", "good-key")
        monkeypatch.setattr(probe, "probe_http_egress", _healthy_egress)
        monkeypatch.setattr(probe, "get_balance", _valid_balance)
        code = probe.main([])
        # proxied_neutral is NO_PROXY -> not healthy -> exit 1.
        assert code == 1
