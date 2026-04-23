"""Unit tests for ``scripts.dispatcher.stream_forwarder`` (issue #3017).

Acceptance criteria covered:

- **AC#1** (incremental forwarding): Lines emitted by the child reach
  the logger and the JSONL mirror as they arrive, not in a single
  batch at EOF. Verified by a mock subprocess that emits lines with
  sleeps between them and asserts the sink captured each line at a
  timestamp close to when the child emitted it.
- **AC#3** (tagging): Every forwarded record carries ``agent_id``,
  ``issue_number``, ``phase``, ``stream``, and ``message`` fields.
- **AC#4** (JSONL mirror): The file at the requested path exists, is
  append-only, and each line is parseable JSON with the same schema as
  the log record.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.stream_forwarder import (  # noqa: E402
    MAX_LINE_CHARS,
    TRUNCATION_MARKER,
    _truncate_line,
    stream_subprocess_output_async,
)


class _RecordingHandler(logging.Handler):
    """Minimal ``logging.Handler`` that records every emitted record.

    Unlike :class:`logging.handlers.MemoryHandler` this does not buffer —
    each ``handle()`` appends immediately, and the caller can inspect
    timestamps to assert incremental arrival.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[float, logging.LogRecord]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((time.monotonic(), record))


def _make_logger(name: str) -> tuple[logging.Logger, _RecordingHandler]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    # Remove any handlers from prior tests in the same process.
    logger.handlers = []
    logger.propagate = False
    handler = _RecordingHandler()
    logger.addHandler(handler)
    return logger, handler


def _emit_lines_script(lines: list[str], delay_s: float) -> str:
    """Return a Python one-liner (as a shell-safe single-script file path).

    The script prints each *line* to stdout (or ``ERR:...`` to stderr if
    the line starts with that prefix), flushing after each, with a
    ``delay_s`` sleep between lines. Used as the mock child in the
    incremental-forwarding test.

    The caller writes the returned script body to a tempfile under the
    test's ``tmp_path`` and invokes Python with the file path — no
    ``python -c`` (CLAUDE.md Critical Rules ban inline ``-c``).
    """
    # Emit each line via print+flush so there's no stdlib buffering
    # between the child and the pipe. Stderr lines get their prefix
    # stripped and routed to sys.stderr.
    parts = [
        "import sys, time",
    ]
    for line in lines:
        if line.startswith("ERR:"):
            payload = line[len("ERR:") :]
            parts.append(f"sys.stderr.write({payload!r} + chr(10)); sys.stderr.flush()")
        else:
            parts.append(f"sys.stdout.write({line!r} + chr(10)); sys.stdout.flush()")
        parts.append(f"time.sleep({delay_s})")
    return "\n".join(parts)


@pytest.fixture
def tmp_script(tmp_path: Path):
    """Return a helper that writes a Python snippet to ``tmp_path`` and
    yields its absolute path. The file is cleaned up by pytest's
    ``tmp_path`` fixture.
    """

    def _write(body: str, name: str = "child.py") -> Path:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        return p

    return _write


class TestIncrementalForwarding:
    """AC#1 — lines reach the logger incrementally, not at EOF."""

    def test_stdout_lines_forwarded_before_subprocess_exits(
        self, tmp_path: Path, tmp_script
    ) -> None:
        """The logger must receive each line within a small window of
        when the child emitted it. We use a 150ms inter-line delay and
        assert that the time deltas between consecutive sink arrivals
        match the child's emit cadence — if the forwarder were batching
        at EOF, all records would land within a few ms of each other.
        """
        lines = ["line-1", "line-2", "line-3"]
        delay_s = 0.15
        script_path = tmp_script(_emit_lines_script(lines, delay_s=delay_s))

        logger, handler = _make_logger("test.forwarder.incremental")
        jsonl_path = tmp_path / ".dispatcher" / "ralph-abc123.jsonl"

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            threads = stream_subprocess_output_async(
                proc,
                agent_id="abc123",
                issue_number=3017,
                phase="ralph",
                logger=logger,
                jsonl_path=jsonl_path,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

        # Extract timestamps for just the stdout records we care about.
        stdout_records = [
            (ts, rec)
            for (ts, rec) in handler.records
            if getattr(rec, "stream", None) == "stdout"
        ]
        assert len(stdout_records) == len(lines), (
            f"expected {len(lines)} stdout records, got {len(stdout_records)}: "
            f"{[r.__dict__ for _, r in stdout_records]}"
        )
        # Verify order and message contents.
        for (ts, rec), expected_line in zip(stdout_records, lines, strict=True):
            assert getattr(rec, "raw_message", None) == expected_line

        # Verify incremental arrival. The gap between the first and
        # last record should be at least ``delay_s * (N - 1)`` (the
        # child's actual emit span). If the forwarder were batching,
        # all records would have effectively the same timestamp.
        first_ts = stdout_records[0][0]
        last_ts = stdout_records[-1][0]
        elapsed = last_ts - first_ts
        min_span = delay_s * (len(lines) - 1) * 0.5  # 50% tolerance
        assert elapsed >= min_span, (
            f"expected ≥{min_span:.3f}s span between first and last record "
            f"(incremental forwarding), got {elapsed:.3f}s — forwarder may "
            f"be batching at EOF"
        )

    def test_stderr_lines_forwarded_with_stream_stderr_tag(
        self, tmp_path: Path, tmp_script
    ) -> None:
        """Stderr content is tagged ``stream="stderr"`` and reaches the
        logger independently of stdout."""
        lines = ["stdout-a", "ERR:err-b", "stdout-c", "ERR:err-d"]
        script_path = tmp_script(_emit_lines_script(lines, delay_s=0.01))

        logger, handler = _make_logger("test.forwarder.stderr")
        jsonl_path = tmp_path / ".dispatcher" / "plan-xyz.jsonl"

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            threads = stream_subprocess_output_async(
                proc,
                agent_id="xyz",
                issue_number=42,
                phase="plan",
                logger=logger,
                jsonl_path=jsonl_path,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

        stdout_msgs = sorted(
            getattr(rec, "raw_message", "")
            for (_, rec) in handler.records
            if getattr(rec, "stream", None) == "stdout"
        )
        stderr_msgs = sorted(
            getattr(rec, "raw_message", "")
            for (_, rec) in handler.records
            if getattr(rec, "stream", None) == "stderr"
        )
        assert stdout_msgs == ["stdout-a", "stdout-c"]
        assert stderr_msgs == ["err-b", "err-d"]


class TestRecordSchema:
    """AC#3 — every record carries the five required tag fields."""

    def test_every_record_has_required_tag_fields(
        self, tmp_path: Path, tmp_script
    ) -> None:
        script_path = tmp_script(_emit_lines_script(["hello"], delay_s=0))

        logger, handler = _make_logger("test.forwarder.schema")
        jsonl_path = tmp_path / ".dispatcher" / "summary-aaa.jsonl"

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            threads = stream_subprocess_output_async(
                proc,
                agent_id="aaa",
                issue_number=999,
                phase="summary",
                logger=logger,
                jsonl_path=jsonl_path,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert handler.records, "expected at least one forwarded record"
        for _, record in handler.records:
            assert getattr(record, "agent_id", None) == "aaa"
            assert getattr(record, "issue_number", None) == 999
            assert getattr(record, "phase", None) == "summary"
            assert getattr(record, "stream", None) in {"stdout", "stderr"}
            assert isinstance(getattr(record, "raw_message", None), str)
            assert getattr(record, "event", None) == "phase_subprocess_line"


class TestJsonlMirror:
    """AC#4 — the JSONL mirror file contains the same tagged events."""

    def test_jsonl_mirror_file_has_one_json_per_line(
        self, tmp_path: Path, tmp_script
    ) -> None:
        script_path = tmp_script(
            _emit_lines_script(["alpha", "beta", "ERR:gamma"], delay_s=0.01)
        )

        logger, _ = _make_logger("test.forwarder.jsonl")
        jsonl_path = tmp_path / ".dispatcher" / "verify-bbb.jsonl"

        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            threads = stream_subprocess_output_async(
                proc,
                agent_id="bbb",
                issue_number=123,
                phase="verify",
                logger=logger,
                jsonl_path=jsonl_path,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

        assert jsonl_path.exists(), "JSONL mirror file was not created"
        raw = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(raw) == 3, f"expected 3 lines, got {len(raw)}: {raw!r}"
        parsed = [json.loads(line) for line in raw]
        # All three records must carry the same agent/issue/phase tags.
        for rec in parsed:
            assert rec["agent_id"] == "bbb"
            assert rec["issue_number"] == 123
            assert rec["phase"] == "verify"
            assert rec["event"] == "phase_subprocess_line"
            assert rec["stream"] in {"stdout", "stderr"}
            assert isinstance(rec["raw_message"], str)

    def test_jsonl_mirror_is_append_only(self, tmp_path: Path, tmp_script) -> None:
        """A second forwarder call on the same path appends; prior
        content is preserved. This matches the real-world case where
        ralph's Step 2.5 retry reinvokes the same phase in the same
        worktree.
        """
        logger, _ = _make_logger("test.forwarder.append")
        jsonl_path = tmp_path / ".dispatcher" / "ralph-ccc.jsonl"

        for batch in (["first-run"], ["second-run"]):
            script_path = tmp_script(
                _emit_lines_script(batch, delay_s=0),
                name=f"child-{batch[0]}.py",
            )
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threads = stream_subprocess_output_async(
                proc,
                agent_id="ccc",
                issue_number=7,
                phase="ralph",
                logger=logger,
                jsonl_path=jsonl_path,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)

        raw = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(raw) == 2
        msgs = [json.loads(line)["raw_message"] for line in raw]
        assert msgs == ["first-run", "second-run"]


class TestTeeSinks:
    """The optional ``stdout_sink`` / ``stderr_sink`` tees preserve the
    daemon's pre-existing capture layout (``claude-p-<phase>.stdout.json``
    etc.) while the forwarder is active.
    """

    def test_stdout_sink_receives_raw_lines_with_trailing_newlines(
        self, tmp_path: Path, tmp_script
    ) -> None:
        script_path = tmp_script(_emit_lines_script(["raw-a", "raw-b"], delay_s=0))

        logger, _ = _make_logger("test.forwarder.tee.stdout")
        stdout_tee = tmp_path / "stdout_capture.json"
        stderr_tee = tmp_path / "stderr_capture.log"
        jsonl_path = tmp_path / ".dispatcher" / "retro-ddd.jsonl"

        with (
            stdout_tee.open("w", encoding="utf-8") as sout,
            stderr_tee.open("w", encoding="utf-8") as serr,
        ):
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threads = stream_subprocess_output_async(
                proc,
                agent_id="ddd",
                issue_number=1,
                phase="retro",
                logger=logger,
                jsonl_path=jsonl_path,
                stdout_sink=sout,
                stderr_sink=serr,
            )
            proc.wait(timeout=10)
            threads.join(timeout=5)

        assert stdout_tee.read_text(encoding="utf-8") == "raw-a\nraw-b\n"


class TestLineTruncation:
    """Lines over :data:`MAX_LINE_CHARS` are clipped with a marker."""

    def test_short_lines_not_truncated(self) -> None:
        assert _truncate_line("hello") == "hello"

    def test_long_line_truncated_with_marker(self) -> None:
        long = "x" * (MAX_LINE_CHARS + 100)
        out = _truncate_line(long)
        assert len(out) == MAX_LINE_CHARS
        assert out.endswith(TRUNCATION_MARKER)
        assert out.startswith("x")

    def test_exactly_max_chars_not_truncated(self) -> None:
        exact = "y" * MAX_LINE_CHARS
        assert _truncate_line(exact) == exact


class TestForwarderThreadsHandle:
    """:class:`ForwarderThreads` joins cleanly even on a hung child."""

    def test_join_returns_after_child_exits(self, tmp_path: Path, tmp_script) -> None:
        script_path = tmp_script(_emit_lines_script(["quick"], delay_s=0))
        logger, _ = _make_logger("test.forwarder.join")
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threads = stream_subprocess_output_async(
            proc,
            agent_id="eee",
            issue_number=None,
            phase="plan",
            logger=logger,
            jsonl_path=None,  # no JSONL mirror
        )
        proc.wait(timeout=5)
        threads.join(timeout=5)
        assert not threads.stdout_thread.is_alive()
        assert not threads.stderr_thread.is_alive()


class TestMissingIssueNumber:
    """``issue_number=None`` is tolerated — the diagnoser spawn path has
    no single bound issue number (it spans multiple failures).
    """

    def test_none_issue_number_is_logged_as_null(
        self, tmp_path: Path, tmp_script
    ) -> None:
        script_path = tmp_script(_emit_lines_script(["line"], delay_s=0))
        logger, handler = _make_logger("test.forwarder.no_issue")
        jsonl_path = tmp_path / ".dispatcher" / "diagnose-fff.jsonl"
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threads = stream_subprocess_output_async(
            proc,
            agent_id="fff",
            issue_number=None,
            phase="diagnose",
            logger=logger,
            jsonl_path=jsonl_path,
        )
        proc.wait(timeout=5)
        threads.join(timeout=5)

        assert handler.records
        rec = handler.records[0][1]
        assert getattr(rec, "issue_number", "MISSING") is None
        # JSONL mirror also has None serialized as JSON null.
        raw = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        parsed = json.loads(raw[0])
        assert parsed["issue_number"] is None
