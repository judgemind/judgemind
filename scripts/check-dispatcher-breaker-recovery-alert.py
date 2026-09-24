#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-dispatcher-breaker-recovery-alert.py — Hygiene guard enforcing
that every config-flag circuit breaker in
``scripts/dispatcher/daemon.py`` declares BOTH a recovery path AND a
Telegram alert path (#4593).

Why this check exists
---------------------
The diagnoser circuit breaker
(``DispatcherDaemon._check_diagnoser_circuit_breaker``) shipped with a
one-way flip of ``dispatcher.config.diagnoser_enabled`` to ``false`` and
no recovery path — once tripped, no diagnoses ran, so the breaker could
never re-measure the fallback rate and re-enable itself (#4586). That is
the same design smell the overnight-safety breaker had before #3779
added time-based auto-close. The pattern recurs for any future breaker
that flips a kill-switch flag without a closing/recovery path AND without
a loud alert.

This guard is the preventative measure: it fails CI when a future
config-flag circuit breaker in ``daemon.py`` is missing a recovery method
OR a Telegram alert method, OR introduces a NEW kill-switch attribution
tag without registering its recovery + alert.

Sibling pattern: ``scripts/check-no-check-prefix-on-ecs-oneshots.py``
(#4563) — a Python guard with a thin ``exec python3`` bash wrapper, plain
regex over the source text (AST is overkill and brittle against
SQL-in-strings), ``--list`` flag, ``--daemon-path`` override for tests,
and a copy-pasteable ``Fix:`` block on failure.

The registry
------------
The guard maintains a declared registry of config-flag circuit breakers
(``_BREAKER_REGISTRY``). Each entry declares:

* ``tag`` — the ``updated_by`` attribution string the breaker uses on its
  trip flip. For the overnight breaker the tag is the value of the
  ``CAP_FLIPPED_BY_CIRCUIT_BREAKER`` constant (``"circuit_breaker"``); for
  the diagnoser breaker the tag is the literal ``"diagnoser_circuit_breaker"``.
* ``recovery_method`` — the name of the auto-recovery method (must exist
  as ``def <name>(`` in daemon.py).
* ``alert_method`` — the name of the Telegram alert method (must exist as
  ``def <name>(`` in daemon.py).

Invariants
----------
**Invariant A — registry completeness** (catches a new unregistered
breaker). Scan daemon.py for every autonomous breaker-attribution write:

* Every ``updated_by = '<tag>'`` SQL-string literal whose ``<tag>`` is
  NOT in the operator-handler allowlist (the operator handlers attribute
  ``updated_by = 'daemon'``) and does NOT end in ``_auto_recover`` /
  ``_auto_close`` (those are recovery-side writes of an already-registered
  breaker, not trip writes).
* The ``CAP_FLIPPED_BY_CIRCUIT_BREAKER = "<value>"`` trip constant's value.

For each discovered trip tag, assert it appears as a registry entry
``tag``. A trip tag with no registry entry => FAIL.

**Invariant B — recovery + alert methods exist** (catches removed/renamed
recovery or alert). For each registry entry, assert daemon.py contains
both ``def <recovery_method>(`` and ``def <alert_method>(``. A missing
method => FAIL.

CLI
---
::

    scripts/check-dispatcher-breaker-recovery-alert.py             # scan, exit 0/1
    scripts/check-dispatcher-breaker-recovery-alert.py --list      # print registry + tags, exit 0
    scripts/check-dispatcher-breaker-recovery-alert.py --daemon-path PATH

Exit codes
----------
* ``0`` — every trip tag is registered AND every registry entry's recovery
  + alert methods exist; OR ``--list`` was passed.
* ``1`` — one or more invariants violated. A ``Fix:`` block names the
  failing tag / method and the remediation.
* ``2`` — script error (daemon.py missing).

Tracking: issue #4593 (root cause: #4586, prior fix: #3779).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BreakerEntry:
    """A declared config-flag circuit breaker and its required paths."""

    name: str
    tag: str
    recovery_method: str
    alert_method: str


# Today's compliant state — two breakers, both with recovery + alert.
_BREAKER_REGISTRY: tuple[BreakerEntry, ...] = (
    BreakerEntry(
        name="overnight-safety",
        tag="circuit_breaker",
        recovery_method="_check_circuit_breaker_auto_close",
        alert_method="_send_circuit_breaker_telegram_alert",
    ),
    BreakerEntry(
        name="diagnoser",
        tag="diagnoser_circuit_breaker",
        recovery_method="_check_diagnoser_breaker_auto_recover",
        alert_method="_send_diagnoser_breaker_telegram_alert",
    ),
)

# Operator-handler attribution tags. These are NOT autonomous breaker
# trips — they are the normal daemon writes (cap bumps, resets, operator
# reflips). ``daemon`` is the only such tag in daemon.py today.
_OPERATOR_ALLOWLIST: frozenset[str] = frozenset({"daemon"})

# ─────────────────────────────────────────────────────────────────────
# Detection regexes
# ─────────────────────────────────────────────────────────────────────

# ``updated_by = '<tag>'`` or ``updated_by = "<tag>"`` SQL-string literal.
# Tolerant of surrounding whitespace and both quote styles. The
# parameterized form (``updated_by = %s``) is intentionally NOT matched —
# its value flows from the ``CAP_FLIPPED_BY_*`` constant, which we detect
# separately.
_UPDATED_BY_RE = re.compile(r"""updated_by\s*=\s*(['"])(?P<tag>[^'"]+)\1""")

# ``CAP_FLIPPED_BY_CIRCUIT_BREAKER = "circuit_breaker"`` — the cap-flipped
# trip constant. We match any ``CAP_FLIPPED_BY_<X> = "<value>"`` assignment
# and treat its value as a breaker trip tag.
_CAP_FLIPPED_CONST_RE = re.compile(
    r"""^\s*CAP_FLIPPED_BY_\w+\s*=\s*(['"])(?P<value>[^'"]+)\1""",
    re.MULTILINE,
)

# Recovery-side attribution tag suffixes. A tag ending in one of these is
# the recovery write of an already-registered breaker, not a new trip.
_RECOVERY_TAG_SUFFIXES: tuple[str, ...] = ("_auto_recover", "_auto_close")


# ─────────────────────────────────────────────────────────────────────
# Source reads
# ─────────────────────────────────────────────────────────────────────


def _read_source(path: Path) -> str:
    """Return the full text of ``path`` (UTF-8)."""

    return path.read_text(encoding="utf-8")


def _is_recovery_tag(tag: str) -> bool:
    """Return True iff ``tag`` is a recovery-side write, not a trip."""

    return any(tag.endswith(suffix) for suffix in _RECOVERY_TAG_SUFFIXES)


def discover_trip_tags(path: Path) -> set[str]:
    """Return the set of autonomous breaker trip tags written by daemon.py.

    Union of two detection sources:

    * ``updated_by = '<tag>'`` literals (excluding the operator allowlist
      and recovery-side ``*_auto_recover`` / ``*_auto_close`` tags).
    * ``CAP_FLIPPED_BY_<X> = "<value>"`` constant values.
    """

    text = _read_source(path)
    tags: set[str] = set()

    for m in _UPDATED_BY_RE.finditer(text):
        tag = m.group("tag")
        if tag in _OPERATOR_ALLOWLIST:
            continue
        if _is_recovery_tag(tag):
            continue
        tags.add(tag)

    for m in _CAP_FLIPPED_CONST_RE.finditer(text):
        tags.add(m.group("value"))

    return tags


def method_defined(path: Path, method_name: str) -> bool:
    """Return True iff daemon.py contains ``def <method_name>(``."""

    text = _read_source(path)
    pattern = re.compile(
        r"^\s*def\s+" + re.escape(method_name) + r"\s*\(", re.MULTILINE
    )
    return bool(pattern.search(text))


# ─────────────────────────────────────────────────────────────────────
# Violation detection
# ─────────────────────────────────────────────────────────────────────


def find_violations(path: Path) -> list[str]:
    """Return human-readable violation strings (empty list when compliant).

    Enforces Invariant A (every trip tag is registered) and Invariant B
    (every registry entry's recovery + alert methods exist).
    """

    violations: list[str] = []
    registered_tags = {entry.tag for entry in _BREAKER_REGISTRY}

    # Invariant A — registry completeness.
    for tag in sorted(discover_trip_tags(path)):
        if tag not in registered_tags:
            violations.append(
                f"Invariant A: trip tag '{tag}' is written by daemon.py but is "
                "not declared in _BREAKER_REGISTRY (a new kill-switch flip with "
                "no registered recovery + alert)."
            )

    # Invariant B — recovery + alert methods exist.
    for entry in _BREAKER_REGISTRY:
        if not method_defined(path, entry.recovery_method):
            violations.append(
                f"Invariant B: breaker '{entry.name}' (tag '{entry.tag}') "
                f"declares recovery method '{entry.recovery_method}' but no "
                f"`def {entry.recovery_method}(` exists in daemon.py."
            )
        if not method_defined(path, entry.alert_method):
            violations.append(
                f"Invariant B: breaker '{entry.name}' (tag '{entry.tag}') "
                f"declares alert method '{entry.alert_method}' but no "
                f"`def {entry.alert_method}(` exists in daemon.py."
            )

    return violations


# ─────────────────────────────────────────────────────────────────────
# Fix block
# ─────────────────────────────────────────────────────────────────────


def _format_fix_block() -> str:
    """Build the copy-pasteable Fix block for an error path."""

    lines: list[str] = []
    lines.append("Fix: every config-flag circuit breaker in daemon.py must")
    lines.append("(1) flip its kill-switch with an `updated_by` / `cap_flipped_by`")
    lines.append("    tag registered in `_BREAKER_REGISTRY`")
    lines.append("    (scripts/check-dispatcher-breaker-recovery-alert.py), AND")
    lines.append("(2) declare a recovery method AND a Telegram alert method that")
    lines.append("    both exist as `def <name>(` in daemon.py.")
    lines.append("")
    lines.append("  - New breaker => add a `BreakerEntry(...)` to")
    lines.append("    `_BREAKER_REGISTRY` with its trip `tag`, `recovery_method`,")
    lines.append("    and `alert_method` names, then implement those two methods.")
    lines.append("  - Missing recovery/alert => add the `def <recovery_method>(` /")
    lines.append("    `def <alert_method>(` method, or fix the name drift between")
    lines.append("    the registry entry and the daemon method.")
    lines.append("")
    lines.append("  A one-way kill-switch flip with no recovery path means the")
    lines.append("  breaker can never re-measure and re-enable itself (#4586 — the")
    lines.append("  diagnoser breaker; #3779 — the overnight-safety breaker's")
    lines.append("  time-based auto-close fix). See #4593 for the full rationale.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _default_daemon_path() -> Path:
    """Return the default daemon.py path relative to this script."""

    return Path(__file__).resolve().parent / "dispatcher" / "daemon.py"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Hygiene guard: every config-flag circuit breaker in "
            "scripts/dispatcher/daemon.py must declare a recovery path AND a "
            "Telegram alert path (#4593)."
        ),
    )
    parser.add_argument(
        "--daemon-path",
        type=Path,
        default=None,
        help=(
            "Path to daemon.py (default: scripts/dispatcher/daemon.py relative "
            "to this script). Used by tests with synthetic fixtures."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_mode",
        help=(
            "Print the declared registry and discovered trip tags, then exit 0 "
            "without scanning for violations."
        ),
    )
    args = parser.parse_args(argv)

    daemon_path = (
        args.daemon_path.resolve()
        if args.daemon_path is not None
        else _default_daemon_path()
    )

    if not daemon_path.is_file():
        print(f"ERROR: daemon.py not found: {daemon_path}", file=sys.stderr)
        return 2

    if args.list_mode:
        print(f"Declared breakers: {len(_BREAKER_REGISTRY)}")
        for entry in _BREAKER_REGISTRY:
            print(
                f"  {entry.name}: tag={entry.tag} "
                f"recovery={entry.recovery_method} alert={entry.alert_method}"
            )
        discovered = sorted(discover_trip_tags(daemon_path))
        print(f"Discovered trip tags: {len(discovered)}")
        for tag in discovered:
            print(f"  {tag}")
        return 0

    violations = find_violations(daemon_path)
    if not violations:
        return 0

    print(
        "FAIL: config-flag circuit breaker(s) in scripts/dispatcher/daemon.py "
        "are missing a recovery path or a Telegram alert path, or flip a "
        "kill-switch with an unregistered tag:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    print("", file=sys.stderr)
    print(_format_fix_block(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
