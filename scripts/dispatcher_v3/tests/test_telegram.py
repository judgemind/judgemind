"""Unit tests for ``dispatcher_v3.telegram``.

Pinned invariants (issue #3883):

- **Reuses v2's secret-handling.** ``send_alert`` invokes
  ``scripts/notify-telegram.sh --message-file <path>`` — same calling
  convention v2 uses. The shell script handles the ``TELEGRAM_BOT_TOKEN``
  fetch from Secrets Manager, so v3 inherits it for free.
- **Best-effort.** A non-zero exit / timeout / missing-script never
  raises out of ``send_alert``; each failure mode emits a structured
  log event so post-incident queries can identify what went wrong.
- **Per-trigger event names.** ``v3_telegram.<trigger>_<suffix>`` so a
  CloudWatch query can rollup by trigger site (breaker_opened vs
  needs_review).
- **Message-file isolation.** The message body never goes in argv —
  same posture as v2's ``_send_circuit_breaker_telegram_alert``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dispatcher_v3.telegram import (
    EVENT_DOMAIN_PREFIX,
    NOTIFY_TELEGRAM_EXIT_CODE_MAPPING,
    map_exit_code,
    render_breaker_opened,
    render_needs_review,
    send_alert,
)


# ---------------------------------------------------------------------------
# map_exit_code
# ---------------------------------------------------------------------------


def test_map_exit_code_known_codes() -> None:
    """Each documented notify-telegram.sh exit code maps to expected event."""
    assert map_exit_code(0) == ("sent", None)
    assert map_exit_code(1) == ("usage_error", None)
    assert map_exit_code(2) == ("all_send_failed", None)
    assert map_exit_code(3) == ("config_missing", "secret_fetch_failed")
    assert map_exit_code(4) == ("config_missing", "empty_bot_token")
    assert map_exit_code(5) == ("config_missing", "empty_user_ids")


def test_map_exit_code_unknown_falls_back_to_nonzero() -> None:
    """An exit code not in the table never resolves to 'sent'."""
    assert map_exit_code(42) == ("nonzero_exit", None)
    assert map_exit_code(-9) == ("nonzero_exit", None)


def test_exit_code_table_matches_v2() -> None:
    """The v3 mapping mirrors v2's so a single CW query rolls them up."""
    # v2's mapping is in scripts/dispatcher/daemon.py — pinned by name
    # here so a future v2 mapping change is a deliberate cross-version
    # decision, not silent drift.
    expected = {
        0: ("sent", None),
        1: ("usage_error", None),
        2: ("all_send_failed", None),
        3: ("config_missing", "secret_fetch_failed"),
        4: ("config_missing", "empty_bot_token"),
        5: ("config_missing", "empty_user_ids"),
    }
    assert NOTIFY_TELEGRAM_EXIT_CODE_MAPPING == expected


# ---------------------------------------------------------------------------
# send_alert — happy path / failure paths
# ---------------------------------------------------------------------------


def _fake_runner(returncode: int, stderr: str = "") -> Any:
    """Build a subprocess fake that always returns ``returncode``.

    Captures the argv it was called with on the closure so the test
    can assert call shape (script path, --message-file flag, etc.).
    """
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout="", stderr=stderr
        )

    return runner, calls


def _make_repo_root(tmp_path: Path, *, with_script: bool = True) -> Path:
    """Stage a minimal repo-root tree under ``tmp_path``."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    if with_script:
        (scripts / "notify-telegram.sh").write_text("#!/bin/sh\n")
    return repo


def test_send_alert_happy_path_returns_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exit 0 → returns (0, 'v3_telegram.<trigger>_sent')."""
    repo = _make_repo_root(tmp_path)
    monkeypatch.setenv("DISPATCHER_V3_REPO_ROOT", str(repo))
    runner, calls = _fake_runner(returncode=0)
    rc, event_name = send_alert(
        message="hello",
        trigger="breaker_opened",
        run_id="run-uuid",
        subprocess_runner=runner,
    )
    assert rc == 0
    assert event_name == f"{EVENT_DOMAIN_PREFIX}.breaker_opened_sent"
    assert len(calls) == 1
    argv = calls[0]
    # Must use --message-file (not inline argv).
    assert "--message-file" in argv
    msg_path = Path(argv[argv.index("--message-file") + 1])
    assert msg_path.read_text() == "hello"


def test_send_alert_skips_when_script_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No notify-telegram.sh on disk → return -1 + skipped_no_script event."""
    repo = _make_repo_root(tmp_path, with_script=False)
    monkeypatch.setenv("DISPATCHER_V3_REPO_ROOT", str(repo))
    runner, calls = _fake_runner(returncode=99)  # never invoked

    rc, event_name = send_alert(
        message="hello",
        trigger="needs_review",
        run_id="run-uuid",
        subprocess_runner=runner,
    )
    assert rc == -1
    assert event_name == f"{EVENT_DOMAIN_PREFIX}.needs_review_skipped_no_script"
    # Subprocess never called.
    assert calls == []


def test_send_alert_maps_exit_3_to_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exit 3 (secret fetch) → config_missing event with reason."""
    repo = _make_repo_root(tmp_path)
    monkeypatch.setenv("DISPATCHER_V3_REPO_ROOT", str(repo))
    runner, _ = _fake_runner(returncode=3, stderr="AccessDenied")
    rc, event_name = send_alert(
        message="boom",
        trigger="breaker_opened",
        run_id="run-uuid",
        subprocess_runner=runner,
    )
    assert rc == 3
    assert event_name == f"{EVENT_DOMAIN_PREFIX}.breaker_opened_config_missing"


def test_send_alert_handles_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.TimeoutExpired → return (-2, '<...>_timeout')."""
    repo = _make_repo_root(tmp_path)
    monkeypatch.setenv("DISPATCHER_V3_REPO_ROOT", str(repo))

    def timeout_runner(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    rc, event_name = send_alert(
        message="boom",
        trigger="breaker_opened",
        run_id="run-uuid",
        subprocess_runner=timeout_runner,
    )
    assert rc == -2
    assert event_name == f"{EVENT_DOMAIN_PREFIX}.breaker_opened_timeout"


def test_send_alert_logs_stderr_tail_on_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero exit attaches stderr_tail to the log extras."""
    repo = _make_repo_root(tmp_path)
    monkeypatch.setenv("DISPATCHER_V3_REPO_ROOT", str(repo))
    runner, _ = _fake_runner(returncode=2, stderr="all sends failed: 502 502 502")
    with caplog.at_level(logging.WARNING, logger="dispatcher_v3.telegram"):
        send_alert(
            message="hi",
            trigger="needs_review",
            run_id="run-uuid",
            subprocess_runner=runner,
        )
    # Find the warning-level event; stderr_tail must be present in extras.
    matching = [rec for rec in caplog.records if "all_send_failed" in rec.message]
    assert matching, "must log a warning event on non-zero exit"
    rec = matching[0]
    assert getattr(rec, "stderr_tail", "").endswith("502")


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_breaker_opened_includes_v3_specific_strings() -> None:
    """The breaker message names concurrency_cap_v3 (not v2's cap)."""
    msg = render_breaker_opened(
        bad_count=2,
        window_size=3,
        window_minutes=60,
        statuses=["failed", "failed", "succeeded"],
    )
    assert "v3" in msg
    assert "concurrency_cap_v3" in msg
    assert "2/3" in msg
    assert "60 min" in msg
    assert "failed, failed, succeeded" in msg


def test_render_needs_review_includes_issue_and_agent() -> None:
    msg = render_needs_review(
        issue_number=4242,
        agent_id="ag-abcd",
        diagnoser_exit_code=1,
        diagnoser_exit_reason="OutOfMemoryError",
    )
    assert "Issue: #4242" in msg
    assert "Agent: ag-abcd" in msg
    assert "Diagnoser exit: 1" in msg
    assert "OutOfMemoryError" in msg


def test_render_needs_review_handles_unknown_exit_code() -> None:
    """diagnoser_exit_code=None renders as ``?``."""
    msg = render_needs_review(
        issue_number=1,
        agent_id="ag-1",
        diagnoser_exit_code=None,
        diagnoser_exit_reason="",
    )
    assert "Diagnoser exit: ?" in msg
