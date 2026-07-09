"""CAPSolver Turnstile API integration.

Solves Cloudflare Turnstile challenges via the CAPSolver API
(https://www.capsolver.com/). This is the deterministic fallback path for
scrapers (e.g. SF civil tentative rulings on ``webapps.sftc.org``) that
encounter interactive-mode Turnstile challenges that stealth-only browser
automation cannot silently auto-solve.

Cost (as of 2026-04): $0.80 / 1000 Turnstile solves. The SF civil scraper
runs twice daily -> ~$0.58/year. Essentially free compared to the data loss
caused by a broken session flow.

API flow (two-step polling):

1. ``POST /createTask`` with ``clientKey`` + ``task.websiteURL`` + ``task.websiteKey``.
   -> returns ``taskId``.
2. ``POST /getTaskResult`` repeatedly until ``status == "ready"``.
   -> returns ``solution.token`` (the Turnstile response token).

The returned token is a short-lived (2-5 min) response token that the caller
injects into the site's form and submits via the site's own callback
(``recaptchaCallBack(token)`` when Turnstile is in ``compat=recaptcha`` mode)
or a plain ``form.submit()``.

Reference: https://docs.capsolver.com/en/guide/captcha/turnstile/

Issue: #2623 (SF civil Turnstile session acquisition).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

# CAPSolver API base URL.
CAPSOLVER_API_BASE = "https://api.capsolver.com"

# CAPSolver balance endpoint path (POST {clientKey}).
CAPSOLVER_BALANCE_PATH = "/getBalance"

# Default overall timeout in seconds for solve_turnstile (the issue notes
# typical solve time is 10-30s; we keep 120s headroom for CAPSolver's
# occasional slow responses).
DEFAULT_TIMEOUT_SECONDS = 120.0

# Seconds between ``getTaskResult`` polls. 3s matches CAPSolver's
# recommended polling interval and keeps total request count < 50 per solve.
POLL_INTERVAL_SECONDS = 3.0

# CAPSolver task type for Turnstile solves without a proxy.
TASK_TYPE_TURNSTILE_PROXYLESS = "AntiTurnstileTaskProxyLess"


@dataclass
class CapsolverBalance:
    """Result of a CAPSolver ``getBalance`` credential-health check."""

    valid: bool
    balance: float | None
    error_code: str | None
    error_description: str | None


async def get_balance(api_key: str | None, *, timeout: float = 30.0) -> CapsolverBalance:
    """Check a CAPSolver key's validity and remaining balance via ``getBalance``.

    ``POST /getBalance {clientKey}``. Semantics:

    * Missing/empty key -> ``valid=False`` without making an API call.
    * ``errorId == 0`` -> ``valid=True`` with ``balance`` from the response.
    * ``errorId != 0`` (e.g. ``ERROR_KEY_DOES_NOT_EXIST``) -> ``valid=False``
      with ``error_code`` / ``error_description`` populated.
    * Any httpx/JSON error -> ``valid=False`` with a detail string.

    Note (#4633): CAPSolver returns HTTP ``400`` with an ``errorId`` body for an
    invalid key, so this deliberately does NOT call ``raise_for_status`` — it
    parses the JSON body regardless of status code to surface ``errorCode``
    distinctly rather than reporting an opaque HTTP failure.

    Args:
        api_key: The CAPSolver API key (``clientKey``).
        timeout: Request timeout in seconds.

    Returns:
        A :class:`CapsolverBalance` describing key validity and balance.
    """
    key = api_key or ""
    if not key:
        return CapsolverBalance(
            valid=False,
            balance=None,
            error_code=None,
            error_description="no api key",
        )

    try:
        async with httpx.AsyncClient(
            base_url=CAPSOLVER_API_BASE,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        ) as client:
            response = await client.post(CAPSOLVER_BALANCE_PATH, json={"clientKey": key})
    except httpx.HTTPError as exc:
        logger.error("capsolver.get_balance_http_error", error=str(exc))
        return CapsolverBalance(
            valid=False,
            balance=None,
            error_code=None,
            error_description=str(exc),
        )

    try:
        data = response.json()
    except ValueError as exc:
        logger.error("capsolver.get_balance_invalid_json", error=str(exc))
        return CapsolverBalance(
            valid=False,
            balance=None,
            error_code=None,
            error_description=f"invalid json: {exc}",
        )

    error_id = data.get("errorId", 0)
    if error_id != 0:
        logger.error(
            "capsolver.get_balance_api_error",
            error_id=error_id,
            error_code=data.get("errorCode"),
            error_description=data.get("errorDescription"),
        )
        return CapsolverBalance(
            valid=False,
            balance=None,
            error_code=data.get("errorCode"),
            error_description=data.get("errorDescription"),
        )

    balance = data.get("balance")
    return CapsolverBalance(
        valid=True,
        balance=balance,
        error_code=None,
        error_description=None,
    )


async def solve_turnstile(
    sitekey: str,
    page_url: str,
    *,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> str | None:
    """Solve a Cloudflare Turnstile challenge via CAPSolver.

    Sends the sitekey and page URL to CAPSolver's ``createTask`` endpoint,
    then polls ``getTaskResult`` until the task is ready. Returns the
    solved Turnstile token or ``None`` on any error or timeout.

    Args:
        sitekey: The Cloudflare Turnstile sitekey (``data-sitekey`` attribute
            on the challenge widget). For ``webapps.sftc.org`` this is
            ``0x4AAAAAAAhseTmUbrk0U5ab``.
        page_url: The URL of the page hosting the challenge (e.g.
            ``https://webapps.sftc.org/captcha/captcha.dll``). Does not need
            to be reachable from CAPSolver; Turnstile's server-side validation
            uses the URL's origin to pair with the sitekey.
        api_key: The CAPSolver API key (``clientKey``). If omitted, falls
            back to the ``CAPSOLVER_API_KEY`` environment variable. If
            neither source yields a key, returns ``None`` immediately without
            making any API calls.
        timeout: Maximum total seconds to wait for a solve (across both
            ``createTask`` and ``getTaskResult`` polls).
        poll_interval: Seconds to wait between ``getTaskResult`` polls.

    Returns:
        The Turnstile response token (a JWT-like string) on success, or
        ``None`` on any failure (missing key, API error, timeout,
        unexpected response shape).
    """
    key = api_key or os.environ.get("CAPSOLVER_API_KEY", "")
    if not key:
        logger.debug(
            "turnstile.solver_skipped",
            reason="no_api_key",
            page_url=page_url,
        )
        return None

    logger.info(
        "turnstile.solver_start",
        page_url=page_url,
        sitekey_prefix=sitekey[:10],
        timeout=timeout,
    )

    async with httpx.AsyncClient(
        base_url=CAPSOLVER_API_BASE,
        timeout=30.0,
        headers={"Content-Type": "application/json"},
    ) as client:
        task_id = await _create_task(client, key, sitekey, page_url)
        if task_id is None:
            return None

        token = await _poll_task_result(
            client,
            key,
            task_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    if token is None:
        logger.warning(
            "turnstile.solver_failure",
            page_url=page_url,
            reason="poll_timeout_or_error",
        )
        return None

    logger.info(
        "turnstile.solver_success",
        page_url=page_url,
        token_prefix=token[:16] if len(token) >= 16 else token,
    )
    return token


async def _create_task(
    client: httpx.AsyncClient,
    api_key: str,
    sitekey: str,
    page_url: str,
) -> str | None:
    """Create a CAPSolver task and return its task ID.

    Args:
        client: The configured async HTTP client.
        api_key: CAPSolver ``clientKey``.
        sitekey: Turnstile sitekey.
        page_url: URL of the page hosting the challenge.

    Returns:
        The task ID string on success, or ``None`` if the request failed
        or CAPSolver returned a non-zero ``errorId``.
    """
    payload = {
        "clientKey": api_key,
        "task": {
            "type": TASK_TYPE_TURNSTILE_PROXYLESS,
            "websiteURL": page_url,
            "websiteKey": sitekey,
        },
    }

    try:
        response = await client.post("/createTask", json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error(
            "turnstile.create_task_http_error",
            error=str(exc),
            page_url=page_url,
        )
        return None

    try:
        data = response.json()
    except ValueError as exc:
        logger.error(
            "turnstile.create_task_invalid_json",
            error=str(exc),
        )
        return None

    error_id = data.get("errorId", 0)
    if error_id != 0:
        logger.error(
            "turnstile.create_task_api_error",
            error_id=error_id,
            error_code=data.get("errorCode"),
            error_description=data.get("errorDescription"),
        )
        return None

    task_id = data.get("taskId")
    if not task_id or not isinstance(task_id, str):
        logger.error(
            "turnstile.create_task_missing_task_id",
            response_keys=list(data.keys()),
        )
        return None

    logger.debug("turnstile.task_created", task_id=task_id)
    return task_id


async def _poll_task_result(
    client: httpx.AsyncClient,
    api_key: str,
    task_id: str,
    *,
    timeout: float,
    poll_interval: float,
) -> str | None:
    """Poll CAPSolver ``getTaskResult`` until the task completes.

    Args:
        client: The configured async HTTP client.
        api_key: CAPSolver ``clientKey``.
        task_id: The task ID returned by ``_create_task``.
        timeout: Maximum total seconds to poll.
        poll_interval: Seconds between polls.

    Returns:
        The Turnstile token on success, or ``None`` on timeout, API error,
        or unexpected response shape.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    payload = {"clientKey": api_key, "taskId": task_id}

    while loop.time() < deadline:
        try:
            response = await client.post("/getTaskResult", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error(
                "turnstile.get_task_result_http_error",
                error=str(exc),
                task_id=task_id,
            )
            return None

        try:
            data = response.json()
        except ValueError as exc:
            logger.error(
                "turnstile.get_task_result_invalid_json",
                error=str(exc),
                task_id=task_id,
            )
            return None

        error_id = data.get("errorId", 0)
        if error_id != 0:
            logger.error(
                "turnstile.get_task_result_api_error",
                error_id=error_id,
                error_code=data.get("errorCode"),
                error_description=data.get("errorDescription"),
                task_id=task_id,
            )
            return None

        status = data.get("status")
        if status == "ready":
            solution = data.get("solution") or {}
            token = solution.get("token")
            if isinstance(token, str) and token:
                return token
            logger.error(
                "turnstile.get_task_result_missing_token",
                task_id=task_id,
                solution_keys=list(solution.keys()) if isinstance(solution, dict) else None,
            )
            return None

        # status == "processing" or "idle" -> continue polling.
        await asyncio.sleep(poll_interval)

    logger.warning(
        "turnstile.get_task_result_timeout",
        task_id=task_id,
        timeout=timeout,
    )
    return None
