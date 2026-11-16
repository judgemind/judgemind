"""Each deterministic rule's code is pinned to its ``RULE_VERSIONS`` entry (#5134).

The raw-key reconciler re-ingests a key whose rulings all failed a
deterministic rule only when that rule's version in
``validation.rule_versions.RULE_VERSIONS`` moves past the one recorded on the
failure (#5027).  The bump is a convention; this test enforces it.  It hashes
each rule's ``validation.deterministic.check_<rule>`` and the helpers and
constants it uses (transitively, in the modules below), and fails when a
rule's code or version no longer matches its pin in :data:`RULE_PINS`.
"""

from __future__ import annotations

import inspect

import pytest
import verdict_fingerprint as vf

import validation.deterministic as deterministic
from validation.rule_versions import RULE_VERSIONS

#: Modules whose functions and constants a rule's check pulls in.
_MODULES = (
    "validation.deterministic",
    "validation.hearing_date_source",
    "framework.title_heuristics",
)

#: Why the last repin without a version bump cannot turn a ``fail`` into a
#: ``flag`` or ``pass`` (a tightening, a refactor).  Replace it on every such
#: repin.
REPIN_REASON = "Baseline pinned at the RULE_VERSIONS of #5027 / #4793 (#5134)."

#: ``rule -> (RULE_VERSIONS[rule], fingerprint of its check)``.
RULE_PINS: dict[str, tuple[int, str]] = {
    "case_number_matches_county_format": (1, "4291b7a7f3c7352d"),
    "case_number_not_unknown": (1, "31ec9551e5d51c46"),
    "hearing_date_in_range": (2, "7689e3a5d26fcb96"),
    "no_cross_case_ruling_text": (1, "ac2e97a79ae01cb7"),
    "no_duplicate_ruling_text": (1, "21558bb864cec97c"),
    "no_html_in_ruling_text": (1, "36dbe46eb9b53721"),
    "no_multiple_adversarial_patterns": (1, "003947e27a739dad"),
    "ruling_text_case_number_marker": (1, "01e6e74759d7d3db"),
    "ruling_text_not_empty": (1, "345a37268a44e38f"),
    "ruling_text_reasonable_length": (1, "ded7c6f0371bd853"),
    "unknown_and_null_title_fail": (1, "96a94fa1870fd54c"),
}


def computed_rule_pins() -> dict[str, tuple[int, str]]:
    """Each rule's current version and code fingerprint."""
    out: dict[str, tuple[int, str]] = {}
    for rule, version in RULE_VERSIONS.items():
        check = getattr(deterministic, f"check_{rule}", None)
        if check is None:
            continue
        fingerprints = vf.closure_fingerprints([check], _MODULES)
        out[rule] = (version, vf.value_fingerprint(sorted(fingerprints.items())))
    return out


def format_rule_pins(pins: dict[str, tuple[int, str]]) -> str:
    """*pins* as a Python dict literal, to paste as the new :data:`RULE_PINS`."""
    lines = [f'    "{rule}": ({v}, "{fp}"),' for rule, (v, fp) in sorted(pins.items())]
    return "{\n" + "\n".join(lines) + "\n}"


def _mismatch_message(rule: str, pinned: tuple[int, str], now: tuple[int, str]) -> str:
    if pinned[0] != now[0] and pinned[1] == now[1]:
        return (
            f"RULE_VERSIONS[{rule!r}] moved from {pinned[0]} to {now[0]} but its "
            "check did not change: repin it (or revert the bump)."
        )
    return (
        f"The check of rule {rule!r} changed (RULE_VERSIONS[{rule!r}] = {now[0]}, "
        f"pinned at {pinned[0]}).  If the change can turn one of its 'fail' "
        f"results into 'flag' or 'pass' (a relaxation), bump RULE_VERSIONS[{rule!r}] "
        "in validation/rule_versions.py and repin; no RULE_VERSION_LIVE_AT entry is "
        "needed, since every row written since migration 73 records its versions "
        "and every unversioned row predates the new version.  A tightening (a new "
        "fail, a stricter threshold) or a refactor needs only a repin, with "
        "REPIN_REASON saying why."
    )


def test_every_rule_has_a_check_and_a_pin() -> None:
    for rule in RULE_VERSIONS:
        assert inspect.isfunction(getattr(deterministic, f"check_{rule}", None)), (
            f"RULE_VERSIONS names {rule!r} but validation.deterministic has no check_{rule}"
        )
    assert set(RULE_PINS) == set(RULE_VERSIONS), (
        "RULE_PINS and RULE_VERSIONS name different rules: pin every rule in "
        "RULE_VERSIONS (a new rule starts at version 1). Current pins:\n"
        + format_rule_pins(computed_rule_pins())
    )


def test_rule_code_and_versions_match_the_pins() -> None:
    computed = computed_rule_pins()
    problems = [
        _mismatch_message(rule, RULE_PINS[rule], computed[rule])
        for rule in sorted(set(RULE_PINS) & set(computed))
        if RULE_PINS[rule] != computed[rule]
    ]
    assert not problems, (
        "\n\n".join(problems) + f"\n\nNew RULE_PINS for {__file__}:\n" + format_rule_pins(computed)
    )


def test_repin_reason_is_given() -> None:
    assert REPIN_REASON.strip()


def test_mismatch_message_names_the_way_out() -> None:
    edited = _mismatch_message("x", (1, "a"), (1, "b"))
    assert "RULE_VERSIONS['x']" in edited
    assert "relaxation" in edited
    assert "REPIN_REASON" in edited
    bumped = _mismatch_message("x", (1, "a"), (2, "a"))
    assert "moved from 1 to 2" in bumped


def test_fingerprint_bites_on_a_rule_edit() -> None:
    """A logic edit changes the fingerprint; a comment or docstring edit
    does not."""
    source = inspect.getsource(deterministic.check_ruling_text_reasonable_length)
    base = vf.source_fingerprint(source)
    assert 'result="flag"' in source
    relaxed = source.replace('result="flag"', 'result="pass"', 1)
    assert vf.source_fingerprint(relaxed) != base
    commented = source.replace("\n", "  # a comment\n", 1)
    redocumented = source.replace("truncation sentinel.", "sentinel length.", 1)
    assert redocumented != source
    assert vf.source_fingerprint(commented) == base
    assert vf.source_fingerprint(redocumented) == base


def test_fingerprint_follows_a_rule_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    before = computed_rule_pins()
    monkeypatch.setattr(
        deterministic,
        "_TRUNCATION_SENTINEL_LENGTH",
        deterministic._TRUNCATION_SENTINEL_LENGTH + 1,
    )
    after = computed_rule_pins()
    changed = sorted(rule for rule in before if before[rule] != after[rule])
    assert "ruling_text_reasonable_length" in changed
