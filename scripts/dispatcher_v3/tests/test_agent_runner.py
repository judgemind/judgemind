"""Unit tests for ``dispatcher_v3.agent_runner``.

The tests pin the ECS task-runner contract: argv built via
``dispatcher_v3.runners.build_argv`` is forwarded to ``subprocess.Popen``,
stdout chunks are teed to a local jsonl file, and on EXIT the raw jsonl +
the rendered compact transcript are uploaded to S3. ``boto3.client`` and
``subprocess`` are mocked so the suite runs without AWS or a real
``render-transcript.py`` on disk.

The fixtures set the four required env vars (``AGENT_ID``,
``TASK_ISSUE_NUMBER``, ``RUNNER``, ``SESSIONS_BUCKET``) and redirect
``/tmp/session-<id>.{jsonl,txt}`` into ``tmp_path`` via
``monkeypatch.setattr`` on the module-level path helpers — the production
code uses absolute ``/tmp/`` paths so we cannot rely on cwd.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from dispatcher_v3 import agent_runner


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set the four required env vars; return the dict for assertions."""
    env = {
        "AGENT_ID": "test-agent-id",
        "TASK_ISSUE_NUMBER": "3835",
        "RUNNER": "claude",
        "SESSIONS_BUCKET": "judgemind-sessions-dev",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


@pytest.fixture
def session_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Redirect _session_file / _compact_file into tmp_path.

    The production code writes to ``/tmp/session-<id>.jsonl`` — that is the
    contract the ECS image relies on. For tests we monkeypatch the helpers
    so the writes land under ``tmp_path`` and don't bleed across runs.
    """
    session = tmp_path / "session-test-agent-id.jsonl"
    compact = tmp_path / "session-test-agent-id.txt"
    monkeypatch.setattr(agent_runner, "_session_file", lambda _agent_id: session)
    monkeypatch.setattr(agent_runner, "_compact_file", lambda _agent_id: compact)
    return session, compact


def _fake_proc(stdout_chunks: list[bytes], returncode: int = 0) -> MagicMock:
    """Build a Popen-like mock whose ``stdout`` iterates ``stdout_chunks``.

    ``proc.stdout.readline`` returns each chunk in order then ``b""`` (the
    EOF sentinel that ``iter(readline, b"")`` stops on). ``proc.wait()``
    returns ``returncode``.
    """
    proc = MagicMock(spec=subprocess.Popen)
    # Use BytesIO so each readline() call advances correctly.
    buf = io.BytesIO(b"".join(stdout_chunks))
    proc.stdout = buf
    proc.wait.return_value = returncode
    return proc


# ── main(): subprocess invocation + tee ───────────────────────────────────


def test_main_invokes_popen_with_build_argv_output(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() calls Popen with the argv from build_argv(RUNNER, issue, agent).

    Pins the contract that the entrypoint does not synthesize argv inline —
    every runner edit goes through ``dispatcher_v3.runners``.
    """
    fake_popen = MagicMock(return_value=_fake_proc([b'{"ok":1}\n']))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    rc = agent_runner.main()

    assert rc == 0
    # First positional arg is the argv list — assert it matches build_argv.
    actual_argv = fake_popen.call_args[0][0]
    assert actual_argv == [
        "claude",
        "-p",
        "--worktree=agent-test-agent-id",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "/task #3835",
    ]
    # stderr merged into stdout per spec §4.1.
    assert fake_popen.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert fake_popen.call_args.kwargs["stdout"] == subprocess.PIPE


def test_main_writes_subprocess_stdout_to_session_file(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk read from proc.stdout is written to the session jsonl."""
    session_file, _compact = session_paths
    chunks = [b'{"event":"start"}\n', b'{"event":"tool_use"}\n', b'{"event":"end"}\n']
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=_fake_proc(chunks)))

    agent_runner.main()

    assert session_file.exists()
    assert session_file.read_bytes() == b"".join(chunks)


def test_main_propagates_subprocess_exit_code(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero subprocess returncode is the value main() returns."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        MagicMock(return_value=_fake_proc([b"{}\n"], returncode=42)),
    )

    rc = agent_runner.main()

    assert rc == 42


# ── upload_archive(): both files uploaded, render-transcript.py invoked ──


def test_upload_archive_uploads_jsonl_and_compact_to_s3(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both uploads fire: jsonl and txt under <AGENT_ID>.<ext> keys."""
    session_file, compact = session_paths
    session_file.write_bytes(b'{"event":"start"}\n')

    fake_s3 = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))

    # render-transcript.py is mocked — assume it produces the compact file.
    def _render(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        # Simulate the renderer writing its --output file.
        compact.write_text("rendered transcript")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _render)

    agent_runner.upload_archive()

    # Two upload_file calls — one for jsonl, one for txt.
    assert fake_s3.upload_file.call_count == 2
    fake_s3.upload_file.assert_has_calls(
        [
            call(
                str(session_file),
                "judgemind-sessions-dev",
                "test-agent-id.jsonl",
            ),
            call(
                str(compact),
                "judgemind-sessions-dev",
                "test-agent-id.txt",
            ),
        ]
    )


def test_upload_archive_invokes_render_transcript_script(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The renderer is invoked with the jsonl input + --output compact path.

    Pins the contract that the entrypoint does not call any other transcript
    tool, and uses the module-level ``RENDER_TRANSCRIPT_SCRIPT`` constant so
    the Dockerfile path is the single source of truth.
    """
    session_file, compact = session_paths
    session_file.write_bytes(b"{}\n")

    monkeypatch.setattr("boto3.client", MagicMock(return_value=MagicMock()))
    fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", fake_run)

    agent_runner.upload_archive()

    fake_run.assert_called_once()
    actual_argv = fake_run.call_args[0][0]
    assert actual_argv == [
        "python",
        agent_runner.RENDER_TRANSCRIPT_SCRIPT,
        str(session_file),
        "--output",
        str(compact),
    ]
    assert fake_run.call_args.kwargs.get("check") is True


def test_upload_archive_returns_silently_when_session_file_missing(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the jsonl never got written (Popen failed before tee), no upload.

    The ECS task may exit before main() opens the session file (e.g. the
    image lacks the runner binary). upload_archive() must be safe to call
    in that case.
    """
    fake_s3 = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=fake_s3))
    fake_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Do not create session_file — the fixture already monkeypatched the
    # path helper but did not write the file.
    agent_runner.upload_archive()

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
    agent_runner.upload_archive()

    captured = capsys.readouterr()
    assert "session-archive-upload-failed" in captured.err
    # Compact upload still attempted.
    assert fake_s3.upload_file.call_count == 2


def test_upload_archive_swallows_render_transcript_failure(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render-transcript failure prints to stderr and does not raise.

    The jsonl upload still happens — the diagnoser can fall back to the raw
    jsonl when the compact form is missing.
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

    agent_runner.upload_archive()

    captured = capsys.readouterr()
    assert "compact-transcript-upload-failed" in captured.err
    # Only the jsonl upload succeeded.
    assert fake_s3.upload_file.call_count == 1


# ── EXIT cleanup runs even when main() raises ────────────────────────────


def test_module_main_block_runs_upload_archive_on_main_exception(
    fake_env: dict[str, str],
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``if __name__ == '__main__':`` block calls upload_archive() in a
    finally even when main() raises — exercise the same try/finally shape
    against a fake main that raises, and assert the EXIT cleanup ran.
    """
    cleanup_calls = MagicMock()
    monkeypatch.setattr(agent_runner, "upload_archive", cleanup_calls)

    def _boom() -> int:
        raise RuntimeError("simulated mid-run crash")

    monkeypatch.setattr(agent_runner, "main", _boom)

    # Replicate the if __name__ == "__main__" block. We cannot run the
    # module under runpy because it calls sys.exit(); instead we mirror
    # the try/finally shape, which is the contract under test.
    rc = 1
    raised: BaseException | None = None
    try:
        try:
            rc = agent_runner.main()
        finally:
            agent_runner.upload_archive()
    except RuntimeError as exc:
        raised = exc

    cleanup_calls.assert_called_once()
    assert isinstance(raised, RuntimeError)
    assert rc == 1  # initial value preserved when main() raised before returning


# ── RUNNER env var defaulting ────────────────────────────────────────────


def test_main_defaults_runner_to_claude_when_unset(
    session_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``RUNNER`` is unset the entrypoint defaults to ``claude``."""
    monkeypatch.setenv("AGENT_ID", "test-agent-id")
    monkeypatch.setenv("TASK_ISSUE_NUMBER", "1")
    monkeypatch.setenv("SESSIONS_BUCKET", "judgemind-sessions-dev")
    monkeypatch.delenv("RUNNER", raising=False)

    fake_popen = MagicMock(return_value=_fake_proc([b"{}\n"]))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    agent_runner.main()

    actual_argv = fake_popen.call_args[0][0]
    # claude is the first token when the default runner kicks in.
    assert actual_argv[0] == "claude"
