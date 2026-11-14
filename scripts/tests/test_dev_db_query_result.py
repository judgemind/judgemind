"""Tests for scripts/dev_db_query_result.py — the framed-result checker (#4781).

The checker is what stops dev-db-query.sh from printing a partial result
as if it were whole. These tests feed it captures shaped like real ECS
Exec output: framed by dev_db_query_runner.py, wrapped in SSM banners,
with pty "\\r\\n" line endings, and cut off at various points.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import dev_db_query_result as result  # noqa: E402
import dev_db_query_runner as runner  # noqa: E402

BANNER = "\nThe Session Manager plugin was installed successfully.\n\nStarting session with SessionId: ecs-execute-command-abc\n"
TRAILER = "\nExiting session with sessionId: ecs-execute-command-abc\n"


def _big_payload(rows: int = 5000) -> str:
    return json.dumps(
        [{"id": f"id-{i:06d}", "case_number": f"25CV{i:06d}"} for i in range(rows)],
        separators=(",", ":"),
    )


def _capture(frame: str, *, crlf: bool = False) -> str:
    text = BANNER + frame + TRAILER
    return text.replace("\n", "\r\n") if crlf else text


class TestFrameMarkersMatchRunner:
    def test_markers_match(self) -> None:
        # The runner is uploaded to the container on its own, so it cannot
        # import the markers; the two copies must agree.
        assert result.FRAME_BEGIN == runner.FRAME_BEGIN
        assert result.FRAME_END == runner.FRAME_END


class TestInline:
    def test_inline_round_trip(self) -> None:
        payload = '[{"n":1}]'
        out = result.resolve_payload(
            _capture(runner.frame_inline(payload)), "us-west-2"
        )
        assert out == payload.encode()

    def test_inline_round_trip_with_pty_crlf(self) -> None:
        payload = '[{"n":1,"s":"a\\nb"}]'
        capture = _capture(runner.frame_inline(payload), crlf=True)
        assert result.resolve_payload(capture, "us-west-2") == payload.encode()

    def test_inline_payload_containing_end_marker_text(self) -> None:
        payload = json.dumps([{"note": result.FRAME_END}], separators=(",", ":"))
        out = result.resolve_payload(
            _capture(runner.frame_inline(payload)), "us-west-2"
        )
        assert out == payload.encode()

    def test_truncated_inline_raises(self) -> None:
        frame = runner.frame_inline('[{"n":1},{"n":2},{"n":3}]')
        cut = frame[: frame.index('{"n":3}')] + "Cannot perform start session: EOF"
        with pytest.raises(result.ResultError, match="cut off"):
            result.resolve_payload(BANNER + cut, "us-west-2")

    def test_altered_inline_raises(self) -> None:
        frame = runner.frame_inline('[{"n":1}]').replace('"n":1', '"n":2')
        with pytest.raises(result.ResultError, match="sha256"):
            result.resolve_payload(_capture(frame), "us-west-2")

    def test_short_inline_raises(self) -> None:
        frame = runner.frame_inline('[{"n":1}]').replace('[{"n":1}]', '[{"n"}]')
        with pytest.raises(result.ResultError, match="bytes"):
            result.resolve_payload(_capture(frame), "us-west-2")


class TestNoFrame:
    def test_bare_json_is_rejected(self) -> None:
        # The pre-#4781 shape: a bare (possibly partial) payload.
        with pytest.raises(result.ResultError, match="no result frame"):
            result.resolve_payload(_capture('[{"id":1},{"id":2'), "us-west-2")

    def test_runner_error_is_surfaced(self) -> None:
        capture = _capture('Database error: relation "nope" does not exist')
        with pytest.raises(result.ResultError) as exc_info:
            result.resolve_payload(capture, "us-west-2")
        assert 'relation "nope" does not exist' in str(exc_info.value)

    def test_empty_capture_is_rejected(self) -> None:
        with pytest.raises(result.ResultError, match="no output"):
            result.resolve_payload("", "us-west-2")

    def test_begin_line_cut_mid_way_is_rejected(self) -> None:
        frame = runner.frame_inline('[{"n":1}]')
        with pytest.raises(result.ResultError, match="no result frame"):
            result.resolve_payload(BANNER + frame[:30], "us-west-2")


class _FakeAws:
    """Stand-in for subprocess.run that serves one S3 object."""

    def __init__(self, uri: str, body: bytes, *, cp_rc: int = 0) -> None:
        self.uri = uri
        self.body = body
        self.cp_rc = cp_rc
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd: list[str], **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        if cmd[:3] == ["aws", "s3", "cp"]:
            assert cmd[3] == self.uri
            if self.cp_rc:
                return subprocess.CompletedProcess(
                    cmd, self.cp_rc, b"", b"AccessDenied"
                )
            return subprocess.CompletedProcess(cmd, 0, self.body, b"")
        if cmd[:3] == ["aws", "s3", "rm"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        raise AssertionError(f"unexpected command {cmd}")


URI = "s3://bucket/tmp/dev-db-query/x.json"


class TestS3:
    def test_large_result_comes_back_whole(self) -> None:
        payload = _big_payload()
        assert len(payload) > 100_000
        fake = _FakeAws(URI, payload.encode())
        with patch.object(result.subprocess, "run", fake):
            out = result.resolve_payload(
                _capture(runner.frame_s3(payload, URI)), "us-west-2"
            )
        assert out == payload.encode()
        assert len(json.loads(out)) == 5000
        assert ["aws", "s3", "rm", URI] == fake.calls[-1][:4]

    def test_truncated_download_raises_and_keeps_object(self) -> None:
        payload = _big_payload(100)
        fake = _FakeAws(URI, payload.encode()[:-10])
        with patch.object(result.subprocess, "run", fake):
            with pytest.raises(result.ResultError, match="bytes"):
                result.resolve_payload(
                    _capture(runner.frame_s3(payload, URI)), "us-west-2"
                )
        assert all(c[:3] != ["aws", "s3", "rm"] for c in fake.calls)

    def test_failed_download_raises(self) -> None:
        payload = _big_payload(100)
        fake = _FakeAws(URI, b"", cp_rc=1)
        with patch.object(result.subprocess, "run", fake):
            with pytest.raises(result.ResultError, match="AccessDenied"):
                result.resolve_payload(
                    _capture(runner.frame_s3(payload, URI)), "us-west-2"
                )

    def test_s3_frame_without_end_marker_raises(self) -> None:
        frame = runner.frame_s3("[]", URI).replace(result.FRAME_END, "")
        with pytest.raises(result.ResultError, match="end marker"):
            result.resolve_payload(_capture(frame), "us-west-2")


class TestMain:
    def test_main_prints_payload_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io

        monkeypatch.setattr(
            sys, "stdin", io.StringIO(_capture(runner.frame_inline("[1]")))
        )
        assert result.main([]) == 0
        captured = capsys.readouterr()
        assert captured.out == "[1]\n"

    def test_main_truncated_exits_nonzero_with_fix_and_empty_stdout(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io

        frame = runner.frame_inline('[{"n":1},{"n":2}]')
        monkeypatch.setattr(sys, "stdin", io.StringIO(BANNER + frame[:-25]))
        assert result.main([]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "incomplete" in captured.err
        assert "Fix:" in captured.err

    def test_download_timeout_raises(self) -> None:
        def boom(cmd: list[str], **_: object) -> None:
            raise subprocess.TimeoutExpired(cmd, 1)

        with patch.object(result.subprocess, "run", boom):
            with pytest.raises(result.ResultError, match="did not finish"):
                result.download_s3(URI, "us-west-2")

    def test_delete_failure_is_only_a_note(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"denied")

        with patch.object(result.subprocess, "run", fail):
            result.delete_s3(URI, "us-west-2")
        assert "could not delete" in capsys.readouterr().err

    def test_digest_helper(self) -> None:
        frame = result.parse_frame(runner.frame_inline("abc"))
        assert frame.sha256 == hashlib.sha256(b"abc").hexdigest()
