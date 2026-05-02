"""Unit tests for ``dispatcher_v3.diagnoser_runner``.

The tests pin the diagnoser ECS-task contract: argv is
``claude -p --output-format stream-json --include-partial-messages
/diagnose-failure $AGENT_ID``, stdout chunks are teed to a local jsonl
file under ``/tmp/diagnoser-<id>.jsonl``, and on EXIT the raw jsonl +
the rendered compact transcript are uploaded to S3 under keys
``diagnoser-<id>.{jsonl,txt}``. ``boto3.client`` and ``subprocess`` are
mocked so the suite runs without AWS or a real ``render-transcript.py``
on disk.

The fixtures set the two required env vars (``AGENT_ID``,
``SESSIONS_BUCKET``) and redirect ``/tmp/diagnoser-<id>.{jsonl,txt}``
into ``tmp_path`` via ``monkeypatch.setattr`` on the module-level path
helpers — the production code uses absolute ``/tmp/`` paths so we
cannot rely on cwd.

Mirrors ``test_agent_runner.py`` deliberately — both runners share the
same EXIT/finally archive contract, so the test shape is identical
modulo the diagnoser-specific argv and S3 keys.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from dispatcher_v3 import diagnoser_runner


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set the two required env vars; return the dict for assertions.

    Unlike ``agent_runner`` the diagnoser does not read
    ``TASK_ISSUE_NUMBER`` (the failed agent's issue is recoverable from
    the agent row via the diagnoser SKILL) or ``RUNNER`` (always
    claude). Only ``AGENT_ID`` and ``SESSIONS_BUCKET`` are required.
    """
    env = {
        "AGENT_ID": "test-agent-id",
        "SESSIONS_BUCKET": "judgemind-sessions-dev",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


@pytest.fixture
def session_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Redirect _session_file / _compact_file into tmp_path.

    The production code writes to ``/tmp/diagnoser-<id>.jsonl`` — that
    is the contract the ECS image relies on. For tests we monkeypatch
    the helpers so the writes land under ``tmp_path`` and don't bleed
    across runs.
    """
    session = tmp_path / "diagnoser-test-agent-id.jsonl"
    compact = tmp_path / "diagnoser-test-agent-id.txt"
    monkeypatch.setattr(diagnoser_runner, "_session_file", lambda _agent_id: session)
    monkeypatch.setattr(diagnoser_runner, "_compact_file", lambda _agent_id: compact)
    return session, compact


def _fake_proc(stdout_chunks: list[bytes], returncode: int = 0) -> MagicMock:
    """Build a Popen-like mock whose ``stdout`` iterates ``stdout_chunks``.

    ``proc.stdout.readline`` returns each chunk in order then ``b""`` (the
    EOF sentinel that ``iter(readline, b"")`` stops on). ``proc.wait()``
    returns ``returncode``.
    """
    proc = MagicMock(spec=subprocess.Popen)
    buf = io.BytesIO(b"".join(stdout_chunks))
    proc.stdout = buf
    proc.wait.return_value = returncode
    return proc


# ── importability ─────────────────────────────────────────────────────────


def test_module_importable() -> None:
    """The module is importable; the public surface includes ``main``,
    ``upload_archive``, ``build_argv``, and ``RENDER_TRANSCRIPT_SCRIPT``.

    Pinned because the F2 task-def's ``command = ["python", "-m",
    "dispatcher_v3.diagnoser_runner"]`` is the runtime contract — if the
    module name or its public callables drift, the diagnoser ECS task
    fails at container start with ``ModuleNotFoundError`` (the issue
    that motivated this whole PR). The taskdef-module-references CI
    guard (test_taskdef_module_references.py) is the structural defense;
    this test is the unit-level readback.
    """
    assert callable(diagnoser_runner.main)
    assert callable(diagnoser_runner.upload_archive)
    assert callable(diagnoser_runner.build_argv)
    assert isinstance(diagnoser_runner.RENDER_TRANSCRIPT_SCRIPT, str)


# ── build_argv: argv shape ────────────────────────────────────────────────


def test_build_argv_contains_diagnose_failure_and_agent_id() -> None:
    """argv carries ``/diagnose-failure <agent_id>`` as a single token.

    Spec §4.2: "the diagnoser task runs ``claude -p
    '/diagnose-failure $AGENT_ID'`` against the same image." The
    SLASH-COMMAND + AGENT_ID pair must travel as ONE positional argv
    token (a single quoted string at the shell level) — splitting them
    into two tokens would make claude treat ``$AGENT_ID`` as a
    workspace name rather than a slash-command argument.
    """
    argv = diagnoser_runner.build_argv("ag-test-uuid")
    assert "/diagnose-failure ag-test-uuid" in argv
    # ``claude`` is the binary, ``-p`` is the prompt-mode flag.
    assert argv[0] == "claude"
    assert "-p" in argv


def test_build_argv_uses_stream_json_with_partial_messages() -> None:
    """argv requests ``--output-format stream-json --include-partial-messages``.

    Same shape as the task-runner's claude invocation (see
    ``dispatcher_v3.runners.RUNNERS["claude"]``) — the diagnoser SKILL's
    tool calls stream into CloudWatch Logs in real time, which is the
    liveness signal the launcher's diagnoser-watch path keys off.
    """
    argv = diagnoser_runner.build_argv("ag-test-uuid")
    assert "--output-format" in argv
    assert "stream-json" in argv
    # Adjacent placement: stream-json must immediately follow --output-format.
    fmt_index = argv.index("--output-format")
    assert argv[fmt_index + 1] == "stream-json"
    assert "--include-partial-messages" in argv


# ── main(): subprocess invocation + tee ───────────────────────────────────


def test_main_invokes_popen_with_diagnose_failure_argv(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() calls Popen with ``build_argv(AGENT_ID)`` and stream-json.

    Pins the contract that the entrypoint passes the agent_id through
    to the slash-command argument verbatim — the diagnoser SKILL reads
    it positionally.
    """
    fake_popen = MagicMock(return_value=_fake_proc([b'{"ok":1}\n']))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    rc = diagnoser_runner.main()

    assert rc == 0
    actual_argv = fake_popen.call_args[0][0]
    assert actual_argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "/diagnose-failure test-agent-id",
    ]
    # stderr merged into stdout per spec §4.1/§4.2 archive contract.
    assert fake_popen.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert fake_popen.call_args.kwargs["stdout"] == subprocess.PIPE


def test_main_writes_subprocess_stdout_to_session_file(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk read from proc.stdout is written to the diagnoser jsonl."""
    session_file, _compact = session_paths
    chunks = [b'{"event":"start"}\n', b'{"event":"tool_use"}\n', b'{"event":"end"}\n']
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=_fake_proc(chunks)))

    diagnoser_runner.main()

    assert session_file.exists()
    assert session_file.read_bytes() == b"".join(chunks)


def test_main_propagates_subprocess_exit_code(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero subprocess returncode is the value main() returns.

    The launcher's diagnoser-watch path keys off this exit code: STOPPED-
    non-zero flips the agent to ``status='needs_review'`` and Telegram-
    alerts (spec §4.2). Without faithful exit-code propagation the
    diagnoser-failure handling is silently broken.
    """
    monkeypatch.setattr(
        subprocess,
        "Popen",
        MagicMock(return_value=_fake_proc([b"{}\n"], returncode=42)),
    )

    rc = diagnoser_runner.main()

    assert rc == 42


# ── upload_archive(): both files uploaded with diagnoser- prefix ─────────


def test_upload_archive_uploads_jsonl_and_compact_to_s3(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both uploads fire under ``diagnoser-<AGENT_ID>.{jsonl,txt}`` keys.

    The ``diagnoser-`` prefix is what distinguishes this archive from
    the task-runner archive (``<agent_id>.{jsonl,txt}``). Different
    prefix → no key collision when the same agent's task-runner and
    diagnoser both archive sessions for the same agent_id.
    """
    session_file, compact = session_paths
    session_file.write_bytes(b'{"event":"start"}\n')

    fake_s3 = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))

    # render-transcript.py is mocked — assume it produces the compact file.
    def _render(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        compact.write_text("rendered transcript")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _render)

    diagnoser_runner.upload_archive()

    # Two upload_file calls — one for jsonl, one for txt; both prefixed.
    assert fake_s3.upload_file.call_count == 2
    fake_s3.upload_file.assert_has_calls(
        [
            call(
                str(session_file),
                "judgemind-sessions-dev",
                "diagnoser-test-agent-id.jsonl",
            ),
            call(
                str(compact),
                "judgemind-sessions-dev",
                "diagnoser-test-agent-id.txt",
            ),
        ]
    )


def test_upload_archive_invokes_render_transcript_script(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer is invoked with the jsonl input + --output compact path.

    Pins the contract that the entrypoint does not call any other
    transcript tool, and uses the module-level ``RENDER_TRANSCRIPT_SCRIPT``
    constant so the Dockerfile path is the single source of truth.
    """
    session_file, compact = session_paths
    session_file.write_bytes(b"{}\n")

    monkeypatch.setattr("boto3.client", MagicMock(return_value=MagicMock()))
    fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", fake_run)

    diagnoser_runner.upload_archive()

    fake_run.assert_called_once()
    actual_argv = fake_run.call_args[0][0]
    assert actual_argv == [
        "python",
        diagnoser_runner.RENDER_TRANSCRIPT_SCRIPT,
        str(session_file),
        "--output",
        str(compact),
    ]
    assert fake_run.call_args.kwargs.get("check") is True
    # check-subprocess-timeouts.sh hygiene gate (#3213).
    assert fake_run.call_args.kwargs.get("timeout") == 120


def test_upload_archive_returns_silently_when_session_file_missing(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the jsonl never got written (Popen failed before tee), no upload.

    The ECS task may exit before main() opens the session file (e.g.
    the image lacks the runner binary). upload_archive() must be safe
    to call in that case — the EXIT cleanup runs unconditionally in a
    finally block.
    """
    fake_s3 = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))
    fake_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", fake_run)

    diagnoser_runner.upload_archive()

    fake_s3.upload_file.assert_not_called()
    fake_run.assert_not_called()


def test_upload_archive_swallows_jsonl_upload_failure(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boto3 upload error on the jsonl prints to stderr and does not raise.

    The compact-transcript step still runs after the jsonl failure.
    """
    session_file, compact = session_paths
    session_file.write_bytes(b"{}\n")

    fake_s3 = MagicMock()
    fake_s3.upload_file.side_effect = [
        RuntimeError("simulated S3 failure"),  # jsonl
        None,  # compact
    ]
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))

    def _render(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        compact.write_text("rendered")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _render)

    # No exception escapes.
    diagnoser_runner.upload_archive()

    captured = capsys.readouterr()
    assert "diagnoser-archive-upload-failed" in captured.err
    # Compact upload still attempted.
    assert fake_s3.upload_file.call_count == 2


def test_upload_archive_swallows_render_transcript_failure(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render-transcript failure prints to stderr and does not raise.

    The jsonl upload still happens — the launcher's diagnoser-watch
    path can fall back to the raw jsonl when the compact form is
    missing.
    """
    session_file, _compact = session_paths
    session_file.write_bytes(b"{}\n")

    fake_s3 = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "render")),
    )

    diagnoser_runner.upload_archive()

    captured = capsys.readouterr()
    assert "diagnoser-compact-transcript-upload-failed" in captured.err
    # Only the jsonl upload succeeded.
    assert fake_s3.upload_file.call_count == 1


# ── EXIT cleanup runs even when main() raises ────────────────────────────


def test_module_main_block_runs_upload_archive_on_main_exception(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``if __name__ == '__main__':`` block calls upload_archive() in a
    finally even when main() raises — exercise the same try/finally
    shape against a fake main that raises, and assert the EXIT cleanup
    ran. The exit code propagated is the subprocess's, not the
    upload's — pinned because the launcher's diagnoser-watch path keys
    off the subprocess exit code.
    """
    cleanup_calls = MagicMock()
    monkeypatch.setattr(diagnoser_runner, "upload_archive", cleanup_calls)

    def _boom() -> int:
        raise RuntimeError("simulated mid-run crash")

    monkeypatch.setattr(diagnoser_runner, "main", _boom)

    rc = 1
    raised: BaseException | None = None
    try:
        try:
            rc = diagnoser_runner.main()
        finally:
            diagnoser_runner.upload_archive()
    except RuntimeError as exc:
        raised = exc

    cleanup_calls.assert_called_once()
    assert isinstance(raised, RuntimeError)
    assert rc == 1  # initial value preserved when main() raised before returning


def test_upload_failure_does_not_override_subprocess_exit_code(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subprocess's exit code propagates even if every upload fails.

    Per AC: "Upload failures don't crash. main() exits with the
    subprocess's exit code, not the upload's." Models the full
    main() + upload_archive() invocation pattern from
    ``if __name__ == "__main__"`` — main returns 7, every upload
    raises, the final returned/propagated rc is still 7.
    """
    session_file, compact = session_paths
    # Pre-populate the session file so upload_archive() attempts the
    # boto3 path (the early-return for missing file would mask this).
    session_file.write_bytes(b"{}\n")

    # Subprocess exited with code 7.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        MagicMock(return_value=_fake_proc([b"{}\n"], returncode=7)),
    )
    # boto3.upload_file always fails.
    fake_s3 = MagicMock()
    fake_s3.upload_file.side_effect = RuntimeError("simulated S3 outage")
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))
    # Renderer also fails so the compact-upload branch raises too.
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "render")),
    )

    rc = 1
    try:
        rc = diagnoser_runner.main()
    finally:
        diagnoser_runner.upload_archive()

    assert rc == 7
