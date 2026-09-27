#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_split_ruling_fields_propagated.py — AST scanner that verifies every
``*SplitRuling`` dataclass field is propagated through the worker's
``_try_<county>_split`` dispatcher (issue #4298).

Driven by ``scripts/check-split-ruling-fields-propagated.sh``.  See that
wrapper for the CI integration story.

Why this check exists
---------------------
``LASplitRuling`` had no ``judge_name`` field for years (#4282).  The
worker's ``_try_la_html_split`` dispatcher carried a stale comment
("LASplitRuling has no judge_name field — preserve whatever the scraper
provided") and nobody noticed the gap until #3732 surfaced misattributed
day-of-bench judges in production.

The latent failure shape: when a contributor adds a new field to a
``*SplitRuling`` dataclass, there is no static check that flags missing
propagation through the worker's split-event builder.  The same shape
applies to ``SDSplitRuling``, ``SplitRuling`` (Fresno), Riverside
``SplitRuling``, and any future ``*SplitRuling`` introduced for new
counties.

The worker is the only write path.  ``scripts/reingest_from_s3.py`` used
to carry its own ``_full_reparse_document`` extracted-dict builder, which
this check also scanned; #4845 collapsed reingest onto
``IngestionWorker.process_event``, so the worker's split dispatchers now
cover both live ingestion and reingest.

What this scan flags
--------------------
For each registered ``*SplitRuling`` dataclass / ``__slots__`` class with
a ``worker_fn`` in ``_DATACLASS_SCOPE``, every non-internal field MUST
appear as a key in that worker function's ``split_event`` dict literal.

Every discovered ``*SplitRuling`` MUST be registered in
``_DATACLASS_SCOPE``, even one with no worker dispatcher (registered with
no ``worker_fn``), so a new dataclass cannot silently skip the check.

Internal fields (``ruling_index``) are excluded — they are loop-control
state, not part of the per-ruling payload.

Known propagation gaps that the check tolerates are listed in
``_KNOWN_PROPAGATION_GAPS`` with explicit issue references.  Adding to
this list requires a TODO with a tracking issue number — the goal is to
keep it empty.

Usage
-----

    python3 scripts/check_split_ruling_fields_propagated.py \\
        [--scraper-framework PATH] \\
        [--worker PATH]

Defaults resolve to the repo's standard locations.  Both flags are
provided so the script can be unit-tested against synthesized inputs.

Exit codes
----------

  0 — All ``*SplitRuling`` fields are propagated through the registered
      worker paths (modulo the documented exclusion list).
  1 — At least one propagation gap was detected.

Output
------
On exit code 1, prints one line per violation in the form:

    VIOLATION: <DataclassName>.<field> missing from <function-name> in <file>

Followed by a one-shot summary line with the total violation count.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — exclusion list + per-dataclass scope
# ---------------------------------------------------------------------------

# Fields that are loop-control / iteration internals, not part of the
# per-ruling event payload.  These are intentionally excluded from the
# propagation check.
_INTERNAL_FIELDS: frozenset[str] = frozenset({"ruling_index"})

# Per-dataclass scope.  Keys are dataclass class names.  Each entry may
# carry:
#   ``worker_fn``  — name of the ``_try_<county>_split`` function in
#                    ``ingestion/worker.py`` that consumes this dataclass.
#                    Omitted means "no worker dispatcher today" — the
#                    dataclass is registered (so it is not flagged as an
#                    unknown ``*SplitRuling``) but there is no function to
#                    check its fields against.
# Any other keys in an entry are ignored.
_DATACLASS_SCOPE: dict[str, dict[str, object]] = {
    "LASplitRuling": {"worker_fn": "_try_la_html_split"},
    "SDSplitRuling": {"worker_fn": "_try_sd_calendar_split"},
    # Fresno + Riverside + SF + Santa Clara all name their dataclass plain
    # ``SplitRuling`` — we disambiguate by source-file path during dataclass
    # discovery.
    "SplitRuling@fresno_tentatives": {"worker_fn": "_try_fresno_pdf_split"},
    "SplitRuling@riverside_tentatives": {"worker_fn": "_try_riverside_pdf_split"},
    "SplitRuling@sf_tentatives": {"worker_fn": "_try_sf_pdf_split"},
    "SplitRuling@sc_tentatives": {"worker_fn": "_try_sc_pdf_split"},
    # CC has no worker dispatcher today, so there is no ``split_event``
    # literal to check ``CCSplitRuling`` against — it is registered only so
    # the unknown-dataclass contract stays satisfied.  (It used to be checked
    # against the reingest path's ``_full_reparse_document``, which #4845
    # removed.)  When CC is wired into worker.py, add
    # ``"worker_fn": "_try_cc_pdf_split"`` here.
    "CCSplitRuling": {},
}

# Known propagation gaps that the check intentionally tolerates.  Each
# entry must reference a tracking issue.  The goal is to keep this empty.
# The check exits 0 when the only violations are whitelisted here, but
# logs a warning so the gaps stay visible.
#
# Schema: dataclass-class-name -> {"worker" -> {field-set}}.
_KNOWN_PROPAGATION_GAPS: dict[str, dict[str, frozenset[str]]] = {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataclassDef:
    """A discovered ``*SplitRuling`` definition."""

    # Unambiguous key for ``_DATACLASS_SCOPE`` lookup. For the two
    # ``SplitRuling`` classes (Fresno, Riverside) this carries the
    # ``@<module-stem>`` suffix to disambiguate.
    key: str
    # The bare class name as it appears in source.
    class_name: str
    file_path: str
    fields: frozenset[str]


@dataclass
class Violation:
    dataclass_name: str
    field: str
    target: str  # "worker" or "scope"
    function_name: str
    file_path: str

    def render(self) -> str:
        return (
            f"VIOLATION: {self.dataclass_name}.{self.field} missing from "
            f"{self.function_name} in {self.file_path}"
        )


# ---------------------------------------------------------------------------
# Discovery: ``*SplitRuling`` dataclasses
# ---------------------------------------------------------------------------


def _is_split_ruling_class(node: ast.ClassDef) -> bool:
    """Return True if the class is a ``*SplitRuling`` definition.

    Both ``@dataclass``-decorated dataclasses (annotated assigns) and
    ``__slots__``-style classes are recognized — both shapes appear in
    today's codebase (LA/SD/CC use ``@dataclass``; Fresno/Riverside use
    ``__slots__``).
    """
    return node.name.endswith("SplitRuling")


def _extract_dataclass_fields(node: ast.ClassDef) -> frozenset[str]:
    """Extract the field names from a ``*SplitRuling`` class definition.

    Handles two shapes:
      1. ``@dataclass`` — fields appear as ``AnnAssign`` nodes
         (``name: type = default``).
      2. ``__slots__ = (...)`` — fields appear as a tuple literal
         assigned to ``__slots__``.
    """
    fields: set[str] = set()

    for stmt in node.body:
        # Shape 1: AnnAssign (``name: type = default``)
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.add(stmt.target.id)
            continue

        # Shape 2: ``__slots__ = (...)``
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "__slots__"
                    and isinstance(stmt.value, (ast.Tuple, ast.List))
                ):
                    for elt in stmt.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            fields.add(elt.value)

    return frozenset(fields)


def discover_dataclasses(scraper_framework_root: Path) -> list[DataclassDef]:
    """Walk ``packages/scraper-framework/src/courts/`` for ``*SplitRuling`` defs.

    Returns one ``DataclassDef`` per discovered class.  When two classes
    share the bare name ``SplitRuling`` (Fresno + Riverside today), each
    gets a unique ``key`` of ``SplitRuling@<module-stem>`` for scope lookup.
    """
    courts_root = scraper_framework_root / "courts"
    if not courts_root.is_dir():
        return []

    found: list[DataclassDef] = []

    for py_file in sorted(courts_root.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_split_ruling_class(node):
                fields = _extract_dataclass_fields(node)
                if not fields:
                    # Empty class body or non-field statements — skip.
                    continue
                bare = node.name
                # Use the disambiguated key when the bare name is shared
                # across multiple files.  ``_DATACLASS_SCOPE`` mirrors this
                # naming for SplitRuling (Fresno) / SplitRuling (Riverside).
                if bare == "SplitRuling":
                    key = f"SplitRuling@{py_file.stem}"
                else:
                    key = bare
                found.append(
                    DataclassDef(
                        key=key,
                        class_name=bare,
                        file_path=str(py_file),
                        fields=fields,
                    )
                )

    return found


# ---------------------------------------------------------------------------
# Discovery: worker ``_try_<county>_split`` functions
# ---------------------------------------------------------------------------


def _split_event_keys_in_function(node: ast.FunctionDef) -> frozenset[str]:
    """Walk *node* looking for ``split_event = {...}`` literal assignments
    and return the union of all string keys assigned across them.

    Both annotated and bare assigns are supported.  A ``{**event_data, ...}``
    spread is recognized but contributes no individual keys (only literal
    keys count toward the propagation check, since ``event_data`` is the
    upstream message payload, not the dataclass).
    """
    keys: set[str] = set()
    for sub in ast.walk(node):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(sub, ast.Assign):
            targets = list(sub.targets)
            value = sub.value
        elif isinstance(sub, ast.AnnAssign):
            targets = [sub.target] if sub.target is not None else []
            value = sub.value
        else:
            continue

        if not any(isinstance(t, ast.Name) and t.id == "split_event" for t in targets):
            continue
        if not isinstance(value, ast.Dict):
            continue

        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            # ``**event_data`` shows up as a None key — skip it.

    return frozenset(keys)


def discover_worker_functions(
    worker_path: Path,
) -> dict[str, frozenset[str]]:
    """Parse ``ingestion/worker.py`` and return a dict mapping
    ``_try_<county>_split`` function names to the set of literal keys
    they assign to ``split_event``.
    """
    if not worker_path.is_file():
        return {}

    tree = ast.parse(worker_path.read_text(encoding="utf-8"), filename=str(worker_path))

    out: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name.startswith("_try_")
            and node.name.endswith("_split")
        ):
            out[node.name] = _split_event_keys_in_function(node)
    return out


# ---------------------------------------------------------------------------
# Fix-block guidance: copy-pasteable patch for scope-table omissions
# ---------------------------------------------------------------------------


def _suggest_worker_fn_name(stem: str, worker_path: Path) -> tuple[str, bool]:
    """Look for an existing ``_try_<county>[_<format>]_split`` function in
    *worker_path* that matches the dataclass module *stem*.

    The convention is ``_try_<county>_<format>_split`` where ``<county>`` is
    typically the module stem with ``_tentatives`` stripped (e.g. stem
    ``sc_tentatives`` → county ``sc``) and ``<format>`` is one of ``pdf`` /
    ``html`` / ``calendar`` (or absent).  If a real match is found, return
    ``(name, True)``; otherwise return a conventional placeholder
    ``(_try_<county>_pdf_split, False)`` so the Fix block is still
    copy-pasteable — the operator just edits the format if needed.
    """
    county = stem.removesuffix("_tentatives")

    if not worker_path.is_file():
        return (f"_try_{county}_pdf_split", False)

    try:
        tree = ast.parse(
            worker_path.read_text(encoding="utf-8"), filename=str(worker_path)
        )
    except SyntaxError:
        return (f"_try_{county}_pdf_split", False)

    prefix = f"_try_{county}_"
    suffix = "_split"
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name.startswith(prefix)
            and node.name.endswith(suffix)
        ):
            return (node.name, True)
    # No match — also try the bare ``_try_<county>_split`` shape (no format
    # token) before falling back to the placeholder.
    bare = f"_try_{county}_split"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == bare:
            return (bare, True)

    return (f"_try_{county}_pdf_split", False)


def _suggest_scope_entry(dc: DataclassDef, worker_path: Path) -> str:
    """Build a copy-pasteable Fix block for a ``*SplitRuling`` dataclass
    that is missing from ``_DATACLASS_SCOPE``.

    The block reproduces the exact dict-literal shape used in the live
    scope table so the operator can paste it in without reformatting.
    """
    # The scope key matches what ``discover_dataclasses`` uses: bare class
    # name, except for plain ``SplitRuling`` which is disambiguated by
    # ``@<module-stem>`` suffix.
    if dc.class_name == "SplitRuling":
        scope_key = f"SplitRuling@{Path(dc.file_path).stem}"
    else:
        scope_key = dc.class_name

    worker_fn, real = _suggest_worker_fn_name(Path(dc.file_path).stem, worker_path)
    worker_comment = (
        "" if real else "  # adjust if the worker hook has a different name"
    )

    lines = [
        "",
        (
            "Fix: Add this entry to _DATACLASS_SCOPE in "
            "scripts/check_split_ruling_fields_propagated.py:"
        ),
        "",
        f'    "{scope_key}": {{',
        f'        "worker_fn": "{worker_fn}",{worker_comment}',
        "    },",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-check
# ---------------------------------------------------------------------------


def _whitelist(target: str, dataclass_key: str) -> frozenset[str]:
    """Return the set of fields whitelisted for *target* on *dataclass_key*."""
    # The whitelist is keyed on the bare class name (e.g. "LASplitRuling").
    # Strip the "@<stem>" suffix if present — Fresno/Riverside SplitRuling
    # classes share the bare name and any whitelist would apply to both.
    bare = dataclass_key.split("@", 1)[0]
    by_target = _KNOWN_PROPAGATION_GAPS.get(bare, {})
    return by_target.get(target, frozenset())


def cross_check(
    dataclasses: list[DataclassDef],
    worker_fns: dict[str, frozenset[str]],
    worker_path: Path,
) -> tuple[list[Violation], list[Violation]]:
    """Cross-reference dataclass fields against the worker's split events.

    Returns ``(blocking, whitelisted)`` — blocking violations cause a
    non-zero exit; whitelisted ones are logged but don't fail the run.
    """
    blocking: list[Violation] = []
    whitelisted: list[Violation] = []

    for dc in dataclasses:
        scope = _DATACLASS_SCOPE.get(dc.key)
        if scope is None:
            # Unknown dataclass — flag as a blocking violation so adding a
            # new ``*SplitRuling`` requires registering it in the scope
            # table.  This is part of the contract the check enforces.
            blocking.append(
                Violation(
                    dataclass_name=dc.class_name,
                    field="<class itself>",
                    target="scope",
                    function_name="_DATACLASS_SCOPE",
                    file_path=__file__,
                )
            )
            continue

        worker_fn = scope.get("worker_fn")
        if not isinstance(worker_fn, str) or not worker_fn:
            # Registered with no worker dispatcher — nothing to check.
            continue

        non_internal = dc.fields - _INTERNAL_FIELDS
        keys = worker_fns.get(worker_fn, frozenset())
        if not keys:
            # Function not found in worker.py at all — that's a hard
            # error.  Either the scope table is wrong or worker.py is
            # corrupt.
            blocking.append(
                Violation(
                    dataclass_name=dc.class_name,
                    field="<function not found>",
                    target="worker",
                    function_name=worker_fn,
                    file_path=str(worker_path),
                )
            )
            continue

        wl = _whitelist("worker", dc.key)
        for f in sorted(non_internal - keys):
            v = Violation(
                dataclass_name=dc.class_name,
                field=f,
                target="worker",
                function_name=worker_fn,
                file_path=str(worker_path),
            )
            if f in wl:
                whitelisted.append(v)
            else:
                blocking.append(v)

    return blocking, whitelisted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_scraper_framework_root() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "packages"
        / "scraper-framework"
        / "src"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every *SplitRuling dataclass field is propagated "
            "through the worker's split dispatchers (issue #4298)."
        ),
    )
    parser.add_argument(
        "--scraper-framework",
        type=Path,
        default=_default_scraper_framework_root(),
        help="Path to packages/scraper-framework/src/ (default: repo root).",
    )
    parser.add_argument(
        "--worker",
        type=Path,
        default=None,
        help=(
            "Path to ingestion/worker.py.  Defaults to "
            "<scraper-framework>/ingestion/worker.py."
        ),
    )
    parser.add_argument(
        "--quiet-whitelisted",
        action="store_true",
        help=(
            "Suppress logging of whitelisted (known-gap) violations.  "
            "Useful in tests where the whitelisted set is expected output."
        ),
    )
    args = parser.parse_args(argv)

    scraper_root: Path = args.scraper_framework
    worker_path: Path = (
        args.worker
        if args.worker is not None
        else (scraper_root / "ingestion" / "worker.py")
    )

    dataclasses = discover_dataclasses(scraper_root)
    if not dataclasses:
        print(
            f"WARNING: No *SplitRuling dataclasses found under {scraper_root}",
            file=sys.stderr,
        )
        # No dataclasses to check — a successful no-op.  The CI wrapper
        # treats this as a pass; the main repo's structure ensures this
        # never happens in practice.
        return 0

    worker_fns = discover_worker_functions(worker_path)

    blocking, whitelisted = cross_check(dataclasses, worker_fns, worker_path)

    if whitelisted and not args.quiet_whitelisted:
        for v in whitelisted:
            print(
                f"  (whitelisted) {v.render()}",
                file=sys.stderr,
            )

    if blocking:
        # Index dataclasses by bare class name so we can look up file_path
        # when emitting the Fix block for ``<class itself>`` (scope-table)
        # violations.  When two SplitRuling classes share the bare name
        # (Fresno + Riverside today), both must be registered in
        # _DATACLASS_SCOPE, so two scope violations fire and each gets its
        # own Fix block.  We map class_name → list[DataclassDef] to handle
        # that case.
        dc_by_name: dict[str, list[DataclassDef]] = {}
        for dc in dataclasses:
            dc_by_name.setdefault(dc.class_name, []).append(dc)

        for v in blocking:
            print(v.render())
            if v.target == "scope":
                # Find the DataclassDef that produced this violation so the
                # Fix block can name the source-file stem (needed for the
                # ``SplitRuling@<stem>`` disambiguator) and probe worker.py
                # for an existing ``_try_<county>_split`` function.
                candidates = dc_by_name.get(v.dataclass_name, [])
                emitted_keys: set[str] = set()
                for dc in candidates:
                    key = f"{dc.class_name}@{Path(dc.file_path).stem}"
                    if key in emitted_keys:
                        continue
                    emitted_keys.add(key)
                    print(_suggest_scope_entry(dc, worker_path), file=sys.stderr)
        print(
            f"\nFound {len(blocking)} *SplitRuling propagation gap(s).  "
            "Either propagate the field through the worker's split_event "
            "dict, or add it to _KNOWN_PROPAGATION_GAPS in "
            "scripts/check_split_ruling_fields_propagated.py with a "
            "tracking issue.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
