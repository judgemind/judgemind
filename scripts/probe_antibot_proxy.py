#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Anti-bot infra health probe — proxy (Bright Data 407 detection) + CAPSolver.

Operator re-verification tool after rotating the residential proxy / CAPSolver
credentials (see #4633 / #4638). In #4633 a lapsed Bright Data residential proxy
credential took down the SD + SF anti-bot scrapers, surfacing only as an opaque
Chromium ``net::ERR_TIMED_OUT`` — the true cause was an HTTP ``407 Auth failed``
on the proxy CONNECT. There was no committed tool to check credential health, so
the investigation had to write a throwaway stdlib probe. This is that probe,
committed.

Runs five checks and prints a per-check status table:

1. direct egress IP (control) — proves AWS/laptop egress itself is healthy.
2. proxied egress via a neutral target (``api.ipify.org``) — isolates *proxy
   auth* from *court blocks*: a neutral non-court target that still fails means
   the proxy itself is rejecting us, not the court.
3. proxied reachability of the SD portal.
4. proxied reachability of the SF captcha gateway.
5. CAPSolver ``getBalance`` — key validity + remaining credit.

Because Bright Data authenticates by customer/zone user:pass embedded in the
proxy URL (not by source IP), a ``407`` is location-independent — the probe is
meaningful to run from anywhere.

Usage::

    scripts/with-secret.sh \\
        -e SD_PROXY_URL=judgemind/proxy/residential \\
        -e CAPSOLVER_API_KEY=judgemind/capsolver/api-key \\
        -- packages/scraper-framework/.venv/bin/python scripts/probe_antibot_proxy.py

Exit code: ``0`` when the proxied-neutral check is HEALTHY AND CAPSolver is
valid; ``1`` otherwise (so it doubles as a credential-health gate). Pass
``--json`` to emit the results as JSON instead of the table.
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass

from framework.proxy_health import (
    EgressProbeResult,
    ProxyAuthStatus,
    probe_http_egress,
)
from framework.turnstile_solver import CapsolverBalance, get_balance

SD_PORTAL_URL = "https://odyroa.sdcourt.ca.gov/portal/"
SF_CAPTCHA_URL = "https://webapps.sftc.org/captcha/captcha.dll"
NEUTRAL_URL = "https://api.ipify.org"

# Check names — stable identifiers used by the exit-code gate.
DIRECT_EGRESS = "direct_egress"
PROXIED_NEUTRAL = "proxied_neutral"
PROXIED_SD_PORTAL = "proxied_sd_portal"
PROXIED_SF_CAPTCHA = "proxied_sf_captcha"
CAPSOLVER_BALANCE = "capsolver_balance"


@dataclass
class CheckResult:
    """One row of the probe status table."""

    name: str
    status: str
    http_status: int | None
    latency_s: float | None
    detail: str


def _egress_to_check(name: str, result: EgressProbeResult) -> CheckResult:
    detail = result.detail
    if result.body:
        detail = f"egress_ip={result.body}"
    return CheckResult(
        name=name,
        status=result.status.value,
        http_status=result.http_status,
        latency_s=result.latency_s,
        detail=detail,
    )


def _balance_to_check(balance: CapsolverBalance) -> CheckResult:
    if balance.valid:
        return CheckResult(
            name=CAPSOLVER_BALANCE,
            status="valid",
            http_status=None,
            latency_s=None,
            detail=f"balance={balance.balance}",
        )
    if balance.error_code:
        detail = f"{balance.error_code}: {balance.error_description}"
    else:
        detail = balance.error_description or "invalid"
    return CheckResult(
        name=CAPSOLVER_BALANCE,
        status="invalid",
        http_status=None,
        latency_s=None,
        detail=detail,
    )


def run_checks(
    proxy_url: str | None,
    capsolver_key: str | None,
    *,
    egress_probe: Callable[..., EgressProbeResult] | None = None,
    balance_fn: Callable[..., object] | None = None,
) -> list[CheckResult]:
    """Run the five anti-bot health checks and return one row each.

    ``egress_probe`` and ``balance_fn`` are injectable so tests can supply fakes
    and avoid any network I/O. ``balance_fn`` is an async coroutine function
    (defaults to :func:`framework.turnstile_solver.get_balance`); it is driven
    via ``asyncio.run`` so ``run_checks`` itself stays synchronous.
    """
    egress_probe = egress_probe or probe_http_egress
    balance_fn = balance_fn or get_balance

    results: list[CheckResult] = []

    # 1. Direct control — no proxy.
    direct = egress_probe(NEUTRAL_URL, proxy_url=None)
    results.append(_egress_to_check(DIRECT_EGRESS, direct))

    # 2-4. Proxied legs (neutral, SD portal, SF captcha).
    proxied_targets = (
        (PROXIED_NEUTRAL, NEUTRAL_URL),
        (PROXIED_SD_PORTAL, SD_PORTAL_URL),
        (PROXIED_SF_CAPTCHA, SF_CAPTCHA_URL),
    )
    for name, url in proxied_targets:
        if proxy_url:
            result = egress_probe(url, proxy_url=proxy_url)
            results.append(_egress_to_check(name, result))
        else:
            results.append(
                CheckResult(
                    name=name,
                    status=ProxyAuthStatus.NO_PROXY.value,
                    http_status=None,
                    latency_s=None,
                    detail="SD_PROXY_URL unset",
                )
            )

    # 5. CAPSolver balance.
    balance = asyncio.run(balance_fn(capsolver_key))
    results.append(_balance_to_check(balance))

    return results


def format_table(results: list[CheckResult]) -> str:
    """Render a fixed-width per-check status table with a header row."""
    headers = ("CHECK", "STATUS", "HTTP", "LATENCY", "DETAIL")
    rows: list[tuple[str, str, str, str, str]] = []
    for r in results:
        rows.append(
            (
                r.name,
                r.status,
                "" if r.http_status is None else str(r.http_status),
                "" if r.latency_s is None else f"{r.latency_s:.2f}s",
                r.detail,
            )
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [_fmt(headers), _fmt(tuple("-" * w for w in widths))]
    lines.extend(_fmt(row) for row in rows)
    return "\n".join(lines)


def _exit_code(results: list[CheckResult]) -> int:
    by_name = {r.name: r for r in results}
    neutral = by_name.get(PROXIED_NEUTRAL)
    balance = by_name.get(CAPSOLVER_BALANCE)
    neutral_ok = neutral is not None and neutral.status == ProxyAuthStatus.HEALTHY.value
    balance_ok = balance is not None and balance.status == "valid"
    return 0 if (neutral_ok and balance_ok) else 1


def main(argv: list[str] | None = None) -> int:
    """Read credentials from env, run the checks, print, and return an exit code."""
    parser = argparse.ArgumentParser(
        description="Anti-bot infra health probe (proxy 407 + CAPSolver)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the results as JSON instead of the status table",
    )
    args = parser.parse_args(argv)

    proxy_url = os.environ.get("SD_PROXY_URL", "")
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    results = run_checks(proxy_url or None, capsolver_key)

    if args.json:
        print(_json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(format_table(results))

    return _exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
