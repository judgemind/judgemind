"""dispatcher-v3 Telegram outbound helper.

Thin wrapper around ``scripts/notify-telegram.sh`` for v3's two outbound
trigger sites (v3 spec §6):

1. **Circuit breaker tripped** — see :mod:`dispatcher_v3.breaker`. The
   breaker calls :func:`send_alert` with the rendered breaker-trip
   message after flipping ``concurrency_cap_v3`` to 0.
2. **Agent marked ``status='needs_review'``** — the launcher's diagnoser
   watch path (see :meth:`Launcher._mark_agent_needs_review`) calls
   :func:`send_alert` with a per-agent message naming the issue and
   the diagnoser exit context.

The v2 daemon already integrates with ``notify-telegram.sh`` (see
``scripts/dispatcher/daemon.py:_send_circuit_breaker_telegram_alert``).
v3 reuses the same secret (``TELEGRAM_BOT_TOKEN`` from Secrets Manager,
fetched by the helper script itself), the same per-exit-code
classification, and the same plaintext-via-``--message-file`` calling
convention so the on-the-wire surface is identical between the two
daemons. This keeps a single Telegram bot token / chat target / message
shape across cohabitation, which matches issue #3883's "reuse v2's
secret-handling and message format" guidance.

Best-effort by design: a Telegram outage / misconfigured secret / API
timeout never blocks the breaker flip or the ``needs_review`` DB
transition. The helper script's exit code is mapped to a structured
log event (mirroring v2's
:func:`dispatcher.daemon._map_notify_telegram_exit_code`) so post-
incident CloudWatch queries can distinguish "sent" vs "config_missing"
vs "all_send_failed" without parsing stderr.

Issue #3883.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

#: Path to the Telegram notification helper, relative to the repo root.
#: Mirrors v2's ``NOTIFY_TELEGRAM_SCRIPT_RELPATH``.
NOTIFY_TELEGRAM_SCRIPT_RELPATH = "scripts/notify-telegram.sh"

#: Hard timeout on the ``notify-telegram.sh`` subprocess. Short enough
#: that a Telegram outage can't stall a terminal transition; long
#: enough that AWS Secrets Manager + curl to the Bot API always fits
#: on a healthy network. Mirrors v2's value verbatim.
NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS = 30

#: Per-exit-code mapping. Same shape as v2's
#: :data:`dispatcher.daemon.NOTIFY_TELEGRAM_EXIT_CODE_MAPPING` so a
#: single CloudWatch query can rollup v2 + v3 events without
#: per-version aliasing. Values are ``(event_suffix, reason)`` tuples;
#: ``reason`` is the free-text discriminator for the
#: ``config_missing`` bucket (exit codes 3/4/5 distinguish secret
#: fetch failure vs empty bot_token vs empty user IDs).
NOTIFY_TELEGRAM_EXIT_CODE_MAPPING: dict[int, tuple[str, str | None]] = {
    0: ("sent", None),
    1: ("usage_error", None),
    2: ("all_send_failed", None),
    3: ("config_missing", "secret_fetch_failed"),
    4: ("config_missing", "empty_bot_token"),
    5: ("config_missing", "empty_user_ids"),
}

#: Domain prefix prepended to ``event_suffix`` to build the full
#: structured log event name. Choosing ``v3_telegram_`` (rather than
#: re-using v2's ``circuit_breaker_telegram_``) keeps the per-trigger
#: filterability — the caller (breaker vs ``needs_review`` site)
#: passes its own sub-domain via the ``trigger`` argument.
EVENT_DOMAIN_PREFIX = "v3_telegram"

log = logging.getLogger("dispatcher_v3.telegram")


def map_exit_code(returncode: int) -> tuple[str, str | None]:
    """Return ``(event_suffix, reason)`` for a ``notify-telegram.sh`` exit code.

    Unknown exit codes return ``("nonzero_exit", None)`` so a future
    code added to the script without updating this mapping falls back
    to the generic warning path — never silently treated as success.
    Mirrors :func:`dispatcher.daemon._map_notify_telegram_exit_code`.
    """
    return NOTIFY_TELEGRAM_EXIT_CODE_MAPPING.get(returncode, ("nonzero_exit", None))


def _resolve_repo_root() -> Path:
    """Resolve the repo root for locating ``scripts/notify-telegram.sh``.

    Production: respects the ``DISPATCHER_V3_REPO_ROOT`` env override
    (set by the v3 ECS task entrypoint). Fallback: walk up from this
    module's directory (``scripts/dispatcher_v3/telegram.py`` →
    ``scripts/dispatcher_v3/`` → ``scripts/`` → repo root). The walk
    matches v2's ``_repo_root_for_notify_script`` shape so tests that
    set ``DISPATCHER_V3_REPO_ROOT`` to a tmp_path under pytest get the
    expected behavior.
    """
    override = os.environ.get("DISPATCHER_V3_REPO_ROOT")
    if override:
        return Path(override)
    # scripts/dispatcher_v3/telegram.py → repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def send_alert(
    *,
    message: str,
    trigger: str,
    run_id: str,
    tmp_dir: Path | None = None,
    subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[int, str]:
    """Send *message* via ``notify-telegram.sh`` (best-effort).

    Returns a ``(returncode, event_name)`` tuple so the caller can act
    on the result if needed (e.g. log the breaker-trip success
    separately from the daemon's normal log stream). The full event
    name follows the convention ``v3_telegram.<trigger>_<event_suffix>``
    so CloudWatch queries can filter by trigger site (e.g.
    ``trigger='breaker_opened'`` vs ``trigger='needs_review'``) AND by
    delivery outcome.

    Args:
        message: Plain-text message body. Written to a temp file and
            passed via ``--message-file`` (so the body never appears in
            ``ps`` listings — same posture as v2's
            :meth:`_send_circuit_breaker_telegram_alert`).
        trigger: Short identifier for the call site, used in the log
            event name. Examples: ``"breaker_opened"``,
            ``"needs_review"``.
        run_id: The dispatcher-v3 run UUID. Threaded into log extras
            so cross-restart correlation works even if the message
            body strips the ID for privacy.
        tmp_dir: Optional override for the directory where the message
            tempfile lives. Defaults to ``<repo-root>/tmp``. Tests
            inject ``tmp_path`` here; the production daemon writes to
            its container-local ``tmp/`` so the path is stable across
            calls (same pattern as v2).
        subprocess_runner: Hook for unit tests. Defaults to
            :func:`subprocess.run`.

    Returns:
        ``(returncode, event_name)``. ``returncode`` is the literal
        ``notify-telegram.sh`` exit code (0 = sent; non-zero = some
        flavor of failure). ``event_name`` is the full structured log
        event the helper emitted (``v3_telegram.<trigger>_sent``,
        ``v3_telegram.<trigger>_config_missing``, etc.). Returns
        ``(-1, "v3_telegram.<trigger>_skipped_no_script")`` when the
        helper script does not exist on disk (best-effort skip — same
        as v2).
    """
    runner = subprocess_runner or subprocess.run
    repo_root = _resolve_repo_root()
    notify_script = repo_root / NOTIFY_TELEGRAM_SCRIPT_RELPATH
    if not notify_script.exists():
        event_name = f"{EVENT_DOMAIN_PREFIX}.{trigger}_skipped_no_script"
        log.info(
            event_name,
            extra={
                "event": event_name,
                "run_id": run_id,
                "trigger": trigger,
                "script_path": str(notify_script),
            },
        )
        return -1, event_name

    # Write message to a tempfile so the body never appears in argv.
    actual_tmp_dir = tmp_dir or (repo_root / "tmp")
    actual_tmp_dir.mkdir(parents=True, exist_ok=True)
    msg_path = actual_tmp_dir / f"v3-telegram-{trigger}-{run_id or 'unknown'}.txt"
    msg_path.write_text(message, encoding="utf-8")

    try:
        result = runner(
            [str(notify_script), "--message-file", str(msg_path)],
            capture_output=True,
            text=True,
            timeout=NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        event_name = f"{EVENT_DOMAIN_PREFIX}.{trigger}_timeout"
        log.warning(
            event_name,
            extra={
                "event": event_name,
                "run_id": run_id,
                "trigger": trigger,
                "timeout_s": NOTIFY_TELEGRAM_SUBPROCESS_TIMEOUT_SECONDS,
            },
        )
        return -2, event_name

    event_suffix, reason = map_exit_code(result.returncode)
    event_name = f"{EVENT_DOMAIN_PREFIX}.{trigger}_{event_suffix}"
    log_extra: dict[str, object] = {
        "event": event_name,
        "run_id": run_id,
        "trigger": trigger,
        "exit_code": result.returncode,
    }
    if reason is not None:
        log_extra["reason"] = reason
    if result.returncode != 0:
        log_extra["stderr_tail"] = (result.stderr or "")[-500:]

    if result.returncode == 0:
        log.info(event_name, extra=log_extra)
    else:
        log.warning(event_name, extra=log_extra)
    return result.returncode, event_name


def render_breaker_opened(
    *,
    bad_count: int,
    window_size: int,
    window_minutes: int,
    statuses: list[str],
) -> str:
    """Render the Telegram alert body for a v3 circuit-breaker open.

    Plain text — ``notify-telegram.sh`` defaults to ``parse_mode=HTML``
    but HTML entities would leak through as literal characters on a
    misconfigured client; the v2 message also uses plaintext for the
    same reason. The recent-status list is comma-joined newest-first
    so the operator can see the cascade pattern at a glance.
    """
    recent = ", ".join(statuses[: min(window_size, 10)]) if statuses else "(none)"
    return (
        "Dispatcher v3 circuit breaker OPENED\n"
        f"{bad_count}/{window_size} of the last terminal outcomes in the "
        f"last {window_minutes} min were bad.\n"
        "concurrency_cap_v3 has been set to 0 — the v3 launcher will "
        "not claim new agents.\n"
        f"Recent statuses (newest first): {recent}\n"
        "Manually flip cap_v3 back to >=1 in the admin cockpit once "
        "you've triaged the underlying failure pattern."
    )


def render_needs_review(
    *,
    issue_number: int,
    agent_id: str,
    diagnoser_exit_code: int | None,
    diagnoser_exit_reason: str,
) -> str:
    """Render the Telegram alert body for an agent ``needs_review`` flip.

    Triggered by the C5 diagnoser-failure path: a v3 agent's diagnoser
    ECS task exited non-zero / OOMed / was reclaimed, so the launcher
    bumps ``status='needs_review'`` and the operator must triage. The
    message names the issue + agent + exit context so the operator can
    open the cockpit (or GitHub issue) directly without first looking
    up the agent.
    """
    reason_tail = (diagnoser_exit_reason or "").strip()[:240] or "(none)"
    code_str = "?" if diagnoser_exit_code is None else str(diagnoser_exit_code)
    return (
        "Dispatcher v3 agent needs review\n"
        f"Issue: #{issue_number}\n"
        f"Agent: {agent_id}\n"
        f"Diagnoser exit: {code_str}\n"
        f"Reason: {reason_tail}\n"
        "Open the admin cockpit or the GitHub issue to triage."
    )
