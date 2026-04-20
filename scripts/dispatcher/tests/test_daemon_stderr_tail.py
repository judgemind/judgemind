"""Unit tests for ``_stderr_tail`` helper and the STRUCTURED_LOG_STDERR_MAX
constant (issue #2902).

Acceptance criteria covered:
- AC#1 — tail returns trailing 4000 chars for >4000 inputs (no ellipsis).
- AC#5 — zero inline [:200] truncations remain in daemon.py.
- AC#6 — STRUCTURED_LOG_STDERR_MAX == 4000 and _stderr_tail uses it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.daemon import STRUCTURED_LOG_STDERR_MAX, _stderr_tail  # noqa: E402


class TestStructuredLogStderrMaxConstant:
    """AC#6 — constant value and usage."""

    def test_structured_log_stderr_max_constant_is_4000(self) -> None:
        """AC#6: STRUCTURED_LOG_STDERR_MAX must be exactly 4000."""
        assert STRUCTURED_LOG_STDERR_MAX == 4000

    def test_constant_larger_than_phase_stderr_tail_max(self) -> None:
        """Structured-log events get more context than the normal phase cap."""
        from dispatcher.daemon import PHASE_STDERR_TAIL_MAX_CHARS

        assert STRUCTURED_LOG_STDERR_MAX > PHASE_STDERR_TAIL_MAX_CHARS


class TestStderrTail:
    """AC#1, AC#6 — _stderr_tail helper behaviour."""

    def test_stderr_tail_returns_full_when_under_cap(self) -> None:
        """Short inputs are returned unchanged."""
        text = "short stderr line"
        assert _stderr_tail(text) == text

    def test_stderr_tail_keeps_trailing_4000_when_over_cap(self) -> None:
        """AC#1: 6000-char input returns the last 4000 chars, no ellipsis."""
        text = "a" * 2000 + "b" * 4000
        result = _stderr_tail(text)
        assert len(result) == 4000
        assert result == "b" * 4000
        assert "…" not in result
        assert "..." not in result

    def test_stderr_tail_exactly_at_cap_returns_full(self) -> None:
        """At exactly the cap boundary the full string is returned."""
        text = "x" * STRUCTURED_LOG_STDERR_MAX
        result = _stderr_tail(text)
        assert result == text

    def test_stderr_tail_decodes_bytes(self) -> None:
        """bytes input is decoded via utf-8 (replace on error)."""
        raw = b"git push failed\n"
        result = _stderr_tail(raw)
        assert isinstance(result, str)
        assert "git push failed" in result

    def test_stderr_tail_decodes_bytes_with_invalid_utf8(self) -> None:
        """Invalid UTF-8 bytes are replaced rather than raising."""
        raw = b"ok\xff\xfebad bytes"
        result = _stderr_tail(raw)
        assert isinstance(result, str)
        assert "ok" in result

    def test_stderr_tail_handles_none(self) -> None:
        """None returns an empty string — safe for use with subprocess.stderr."""
        assert _stderr_tail(None) == ""

    def test_stderr_tail_handles_empty_string(self) -> None:
        """Empty string returns empty string."""
        assert _stderr_tail("") == ""

    def test_stderr_tail_strips_surrounding_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped."""
        result = _stderr_tail("  hello world  ")
        assert result == "hello world"

    def test_stderr_tail_trailing_bias(self) -> None:
        """For long inputs the tail (not the head) is returned."""
        head = "HEAD: " + "h" * 3000
        tail = "TAIL: " + "t" * 3000
        long_text = head + "\n" + tail
        result = _stderr_tail(long_text)
        assert "TAIL:" in result
        assert "HEAD:" not in result


class TestNoInline200CharTruncations:
    """AC#5 — no inline [:200] slice on stderr/stdout remains in daemon.py."""

    def test_no_inline_200char_truncations_remain_in_daemon_py(self) -> None:
        """Source-grep assertion: grep for the [:200] pattern must return no matches."""
        daemon_path = Path(__file__).resolve().parents[1] / "daemon.py"
        assert daemon_path.exists(), f"daemon.py not found at {daemon_path}"
        source = daemon_path.read_text(encoding="utf-8")
        # Match stderr/stdout tail slices — the pattern we eliminated.
        matches = re.findall(r"\[:200\]", source)
        assert matches == [], (
            f"Found {len(matches)} inline [:200] truncation(s) in daemon.py — "
            "all call sites should use _stderr_tail() instead. "
            f"Lines:\n"
            + "\n".join(
                f"  {i + 1}: {line.strip()}"
                for i, line in enumerate(source.splitlines())
                if "[:200]" in line
            )
        )
