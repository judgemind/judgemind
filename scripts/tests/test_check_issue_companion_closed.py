# venv: none
"""Tests for ``scripts/_check_issue_companion_closed_inspect.py``.

The companion-closed obsoletion probe (issue #4557) detects when an
issue's body cites a sibling #N that is now closed-completed AND the
cite appears in companion framing ("when #N", "after #N", "removed
when #N", etc.). Used by /task §4a.4 to short-circuit running ralph
on a temporary-caveat docstring whose blocker has already landed.

These tests cover the four AC scenarios:

  1. Closed sibling + matching framing → exit 0 with companion-closed:N.
  2. Open sibling → exit 1 (no-closed-completed).
  3. Closed-as-not-planned sibling → exit 1 (no-closed-completed).
  4. Hashtag without companion framing → exit 1 (no-companion).

Plus a fifth: paragraph-scoped framing — a framing keyword in one
paragraph does not pull in `#N` cites in another paragraph.

Loads the helper module via ``importlib.util.spec_from_file_location``
because the filename starts with an underscore (mirrors the
test_check_near_duplicate_issue.py harness).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_issue_companion_closed_inspect.py"


def _import_probe_module():
    """Load the companion-closed inspector as ``check_issue_companion_closed_inspect``."""
    spec = importlib.util.spec_from_file_location(
        "check_issue_companion_closed_inspect", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_issue_companion_closed_inspect"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe_module():
    return _import_probe_module()


# ─── Layer 1: helper functions ────────────────────────────────────────────


def test_split_paragraphs_blank_line_delimited(probe_module):
    """Paragraphs are split on one or more blank lines."""
    body = "Para A line 1\nPara A line 2\n\nPara B\n\n\nPara C\n"
    out = probe_module._split_paragraphs(body)
    assert out == ["Para A line 1\nPara A line 2", "Para B", "Para C"]


def test_split_paragraphs_handles_crlf(probe_module):
    """Windows / GitHub-style CRLF is normalized to LF."""
    body = "A\r\n\r\nB\r\n"
    out = probe_module._split_paragraphs(body)
    assert out == ["A", "B"]


def test_split_paragraphs_empty_body(probe_module):
    """Empty body yields an empty list."""
    assert probe_module._split_paragraphs("") == []
    assert probe_module._split_paragraphs("   \n\n   ") == []


def test_cites_in_chunk_finds_all_hashtags(probe_module):
    """All ``#N`` tokens are captured in order."""
    chunk = "see #4408 and also #4409 and #99999 and ##42 and #abc"
    out = probe_module._cites_in_chunk(chunk)
    # ``##42`` is matched as #42 by the bash-conventional rule (the
    # second # followed by a digit). #abc is not matched (non-digit).
    assert out == ["4408", "4409", "99999", "42"]


def test_has_companion_framing_when(probe_module):
    """``when`` keyword fires."""
    assert probe_module._has_companion_framing(
        "removed when the structural fix in #4408 lands"
    )


def test_has_companion_framing_after(probe_module):
    """``after`` keyword fires."""
    assert probe_module._has_companion_framing("filed after #4408")


def test_has_companion_framing_until(probe_module):
    """``until`` keyword fires."""
    assert probe_module._has_companion_framing("kept until #4408 closes")


def test_has_companion_framing_once(probe_module):
    """``once`` keyword fires."""
    assert probe_module._has_companion_framing("once #4408 ships, remove this")


def test_has_companion_framing_blocked_on(probe_module):
    """``blocked on`` (multi-word) fires."""
    assert probe_module._has_companion_framing("this is blocked on #4408")


def test_has_companion_framing_blocked_by(probe_module):
    """``blocked by`` (multi-word) fires."""
    assert probe_module._has_companion_framing("this is blocked by #4408")


def test_has_companion_framing_depends_on(probe_module):
    """``depends on`` (multi-word) fires."""
    assert probe_module._has_companion_framing("this depends on #4408 landing")


def test_has_companion_framing_no_match(probe_module):
    """Bare cites without framing keywords don't fire."""
    assert not probe_module._has_companion_framing("see #4408 for context")
    assert not probe_module._has_companion_framing("Closes #4408 and #4409")
    assert not probe_module._has_companion_framing("Parent: #4408")


def test_has_companion_framing_word_boundary(probe_module):
    """Substring matches don't fire (``onceupon`` does not match ``once``)."""
    # All eight keywords as substrings of longer words — none should match.
    assert not probe_module._has_companion_framing("onceupon a time #4408")
    assert not probe_module._has_companion_framing("aftermath #4408")
    assert not probe_module._has_companion_framing("untilage #4408")


def test_is_closed_completed_true(probe_module):
    """closed + COMPLETED is the only firing case."""
    assert probe_module._is_closed_completed(
        {"state": "closed", "stateReason": "COMPLETED"}
    )


def test_is_closed_completed_lowercase(probe_module):
    """Lowercase enum values are accepted (defensive)."""
    assert probe_module._is_closed_completed(
        {"state": "closed", "stateReason": "completed"}
    )


def test_is_closed_completed_open_returns_false(probe_module):
    """Open siblings never fire."""
    assert not probe_module._is_closed_completed({"state": "open", "stateReason": None})
    assert not probe_module._is_closed_completed(
        {"state": "open", "stateReason": "COMPLETED"}
    )


def test_is_closed_completed_not_planned_returns_false(probe_module):
    """closed + NOT_PLANNED does not fire — the work isn't coming."""
    assert not probe_module._is_closed_completed(
        {"state": "closed", "stateReason": "NOT_PLANNED"}
    )


def test_is_closed_completed_missing_state_reason_returns_false(probe_module):
    """closed but no stateReason is ambiguous — do not fire."""
    assert not probe_module._is_closed_completed({"state": "closed"})
    assert not probe_module._is_closed_completed(
        {"state": "closed", "stateReason": None}
    )


def test_is_closed_completed_non_dict_returns_false(probe_module):
    """Bad input shapes never fire (defensive)."""
    assert not probe_module._is_closed_completed(None)
    assert not probe_module._is_closed_completed("closed")
    assert not probe_module._is_closed_completed([])


# ─── Layer 2: end-to-end main() ───────────────────────────────────────────


def _run_main(
    probe_module, body: str, sibling_states: dict[str, dict[str, str]]
) -> tuple[int, str]:
    """Run main() with a stdin body + env-var sibling state map.

    Returns ``(exit_code, stdout)``.
    """
    fake_in = io.StringIO(body)
    fake_out = io.StringIO()
    with (
        mock.patch.dict(
            "os.environ",
            {"SIBLING_STATES_JSON": json.dumps(sibling_states)},
            clear=False,
        ),
        mock.patch.object(sys, "stdin", fake_in),
        mock.patch.object(sys, "stdout", fake_out),
    ):
        exit_code = probe_module.main()
    return exit_code, fake_out.getvalue().strip()


# AC #1: closed sibling + matching framing → exit 0 with companion-closed:N
def test_main_canonical_4409_4408(probe_module):
    """The #4409 ↔ #4408 worked example from issue #4557."""
    body = (
        "## Summary\n\n"
        "Update the docstring at `db.py:1212-1267`.\n\n"
        "This is a temporary caveat that should be removed when the "
        "structural fix in #4408 lands.\n\n"
        "## References\n\n"
        "- See also #9999 for context.\n\n"
        "Parent: #4397\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {
            "4408": {"state": "closed", "stateReason": "COMPLETED"},
            "4397": {"state": "open", "stateReason": None},
            "9999": {"state": "closed", "stateReason": "COMPLETED"},
        },
    )
    assert exit_code == 0
    assert stdout == "companion-closed:4408"


# AC #2: open sibling → exit 1 (no-closed-completed)
def test_main_open_sibling(probe_module):
    """Open sibling does not fire even with companion framing."""
    body = (
        "This is a temporary caveat that should be removed when the "
        "structural fix in #4408 lands.\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "open", "stateReason": None}},
    )
    assert exit_code == 0
    assert stdout == "clear:no-closed-completed"


# AC #3: closed-as-not-planned sibling → exit 1 (no-closed-completed)
def test_main_not_planned_sibling(probe_module):
    """closed + NOT_PLANNED does not fire — work isn't landing."""
    body = (
        "This is a temporary caveat that should be removed when the "
        "structural fix in #4408 lands.\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "closed", "stateReason": "NOT_PLANNED"}},
    )
    assert exit_code == 0
    assert stdout == "clear:no-closed-completed"


# AC #4: hashtag without companion framing → exit 1 (no-companion)
def test_main_bare_hashtag_no_framing(probe_module):
    """``see #4408 for context`` does not fire even when #4408 is closed."""
    body = "## Summary\n\nAdd a new feature.\n\nSee #4408 for context.\n"
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "closed", "stateReason": "COMPLETED"}},
    )
    assert exit_code == 0
    assert stdout == "clear:no-companion"


# AC #4 variant: Closes #N is not framing
def test_main_closes_keyword_does_not_fire(probe_module):
    """``Closes #N`` is informational, not companion framing."""
    body = "## Summary\n\nAdd a new feature.\n\nCloses #4408\n"
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "closed", "stateReason": "COMPLETED"}},
    )
    assert exit_code == 0
    assert stdout == "clear:no-companion"


# AC #4 variant: Parent: #N is not framing
def test_main_parent_line_does_not_fire(probe_module):
    """``Parent: #N`` is hierarchy, not companion framing."""
    body = "## Summary\n\nAdd a new feature.\n\nParent: #4408\n"
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "closed", "stateReason": "COMPLETED"}},
    )
    assert exit_code == 0
    assert stdout == "clear:no-companion"


# AC #5: no #N references at all → exit 1 (no-references)
def test_main_no_references(probe_module):
    """Body with no hashtags returns clear:no-references."""
    body = "## Summary\n\nAdd a new feature with no cross-references.\n"
    exit_code, stdout = _run_main(probe_module, body, {})
    assert exit_code == 0
    assert stdout == "clear:no-references"


# Paragraph scoping: a framing keyword in one paragraph doesn't pull in
# #N cites from a different paragraph.
def test_main_paragraph_scoped_framing(probe_module):
    """``after`` in para 1 + ``#4408`` in para 2 does not fire."""
    body = (
        "Some narrative about doing things after we deploy.\n\nSee #4408 for context.\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {"4408": {"state": "closed", "stateReason": "COMPLETED"}},
    )
    assert exit_code == 0
    # ``after`` is in para 1 (no cite); ``#4408`` is in para 2 (no
    # framing keyword). No paragraph has both, so no fire.
    assert stdout == "clear:no-companion"


# Document-order picker: when multiple cites are companion-framed and
# closed-completed, surface the FIRST in document order (matches the
# AC's #4409 → #4408 expectation).
def test_main_picks_first_in_document_order(probe_module):
    """Document order is the picker, not ascending issue number."""
    body = (
        "This is a temporary caveat that should be removed when the "
        "structural fix in #9999 lands.\n\n"
        "Filed after #1000 for context.\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {
            "9999": {"state": "closed", "stateReason": "COMPLETED"},
            "1000": {"state": "closed", "stateReason": "COMPLETED"},
        },
    )
    assert exit_code == 0
    # #9999 appears first in the body even though it's the larger
    # number — document order picker prefers it.
    assert stdout == "companion-closed:9999"


# Malformed SIBLING_STATES_JSON → exit 1
def test_main_malformed_env_var(probe_module):
    """Malformed JSON in SIBLING_STATES_JSON returns exit 1."""
    fake_in = io.StringIO("body")
    with (
        mock.patch.dict(
            "os.environ",
            {"SIBLING_STATES_JSON": "{not json"},
            clear=False,
        ),
        mock.patch.object(sys, "stdin", fake_in),
    ):
        exit_code = probe_module.main()
    assert exit_code == 1


# Empty SIBLING_STATES_JSON env var defaults to {} (no fire)
def test_main_empty_env_var_no_references(probe_module):
    """Unset SIBLING_STATES_JSON env var → main treats as empty map."""
    body = "## Summary\n\nAdd a feature.\n"
    fake_in = io.StringIO(body)
    fake_out = io.StringIO()
    # Remove the env var entirely.
    with (
        mock.patch.dict("os.environ", {}, clear=False),
        mock.patch.object(sys, "stdin", fake_in),
        mock.patch.object(sys, "stdout", fake_out),
    ):
        # Defensive cleanup — clear=False preserves existing keys.
        import os

        os.environ.pop("SIBLING_STATES_JSON", None)
        exit_code = probe_module.main()
    assert exit_code == 0


# Reproducer for the #4557 AC's "running it against #4409 emits
# companion-closed:4408": with the actual #4409 body shape, we surface
# #4408 (the strongest framing match), not #4386 / #4394 (later
# narrative cites with weaker framing).
def test_main_reproducer_4409_picks_4408_not_later_cites(probe_module):
    """The canonical AC test: #4409 surfaces #4408, not later #4386."""
    body = (
        "## Summary\n\n"
        "Update the docstring.\n\n"
        "This is a temporary caveat that should be removed when the "
        "structural fix in #4408 lands.\n\n"
        "## Background\n\n"
        "Investigation #4397 found that the residual rows after PR "
        "#4394 landed (#4386 fix) were caused by exactly this race.\n"
    )
    exit_code, stdout = _run_main(
        probe_module,
        body,
        {
            "4408": {"state": "closed", "stateReason": "COMPLETED"},
            "4397": {"state": "open", "stateReason": None},
            "4394": {"state": "closed", "stateReason": "COMPLETED"},
            "4386": {"state": "closed", "stateReason": "COMPLETED"},
        },
    )
    assert exit_code == 0
    assert stdout == "companion-closed:4408"
