#!/usr/bin/env python3
# venv: none
# permanent: true
"""check_split_ruling_fields_propagated.py — AST scanner that verifies every
``*SplitRuling`` dataclass field is propagated through the worker's
``_try_<county>_split`` dispatcher AND the reingest path's
``_full_reparse_document`` (issue #4298).

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
propagation through the worker's split-event builder or the reingest's
extracted-dict builder.  The same shape applies to ``SDSplitRuling``,
``SplitRuling`` (Fresno), Riverside ``SplitRuling``, and any future
``*SplitRuling`` introduced for new counties.

What this scan flags
--------------------
For each registered ``*SplitRuling`` dataclass / ``__slots__`` class:

  1. Every non-internal field MUST appear as a key in the corresponding
     worker function's ``split_event`` dict literal (or, for dataclasses
     with no worker function, this check is skipped — see
     ``_DATACLASS_SCOPE``).
  2. Every non-internal field MUST appear in the reingest path's
     ``_full_reparse_document`` either as a key in the ``extracted`` dict
     literal OR as a direct ``ruling.<field>`` attribute access OR as a
     ``getattr(ruling, "<field>", ...)`` access on the loop variable.

Internal fields (``ruling_index``) are excluded — they are loop-control
state, not part of the per-ruling payload.

Known propagation gaps that the check tolerates today are listed in
``_KNOWN_PROPAGATION_GAPS`` with explicit issue references.  Adding to
this list requires a TODO with a tracking issue number — the goal is to
shrink it to empty over time as the gaps are closed.

Usage
-----

    python3 scripts/check_split_ruling_fields_propagated.py \\
        [--scraper-framework PATH] \\
        [--reingest PATH]

Defaults resolve to the repo's standard locations.  Both flags are
provided so the script can be unit-tested against synthesized inputs.

Exit codes
----------

  0 — All ``*SplitRuling`` fields are propagated through the registered
      worker + reingest paths (modulo the documented exclusion list).
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

# Per-dataclass scope: which paths (worker, reingest) the check should
# verify.  Some dataclasses have no worker dispatcher today (CCSplitRuling
# is LLM-only via the reingest registry), and the check must not flag
# them as missing from a function that doesn't exist.  Keys are
# dataclass class names.
#
# Each entry is a dict with two optional keys:
#   ``worker_fn``  — name of the ``_try_<county>_split`` function in
#                    ``ingestion/worker.py`` that consumes this dataclass.
#                    Omitted means "no worker dispatcher today; reingest
#                    only".
#   ``reingest``   — True if the dataclass flows through
#                    ``scripts/reingest_from_s3.py::_full_reparse_document``.
#                    All known dataclasses do; this defaults to True.
_DATACLASS_SCOPE: dict[str, dict[str, object]] = {
    "LASplitRuling": {"worker_fn": "_try_la_html_split", "reingest": True},
    "SDSplitRuling": {"worker_fn": "_try_sd_calendar_split", "reingest": True},
    # Fresno + Riverside + SF all name their dataclass plain ``SplitRuling`` —
    # we disambiguate by source-file path during dataclass discovery.
    "SplitRuling@fresno_tentatives": {
        "worker_fn": "_try_fresno_pdf_split",
        "reingest": True,
    },
    "SplitRuling@riverside_tentatives": {
        "worker_fn": "_try_riverside_pdf_split",
        "reingest": True,
    },
    "SplitRuling@sf_tentatives": {
        "worker_fn": "_try_sf_pdf_split",
        "reingest": True,
    },
    # CC has no worker dispatcher today — its split path runs only via the
    # reingest LLM split registry.  When CC is wired into worker.py, add
    # ``"worker_fn": "_try_cc_pdf_split"`` here.
    "CCSplitRuling": {"reingest": True},
}

# Known propagation gaps that the check intentionally tolerates today.
# Each entry must reference a tracking issue.  The goal is to shrink this
# to empty over time.  The check exits 0 when the only violations are
# whitelisted here, but logs a warning so the gaps stay visible.
#
# Schema: dataclass-class-name -> {target -> {field-set}} where target is
# "worker" or "reingest".
#
# Note: ``parties`` is intentionally NOT whitelisted.  ``_full_reparse_document``
# DOES carry the ``parties`` key in its extracted-dict literal (hardcoded to
# ``[]``), so this check — which scans for field-name propagation, not for
# whether the propagated value is non-empty — sees it as present.  The
# semantic gap (per-case parties from the splitter are dropped and refilled
# by LLM enrichment) is a separate concern and out of scope for this check.
_KNOWN_PROPAGATION_GAPS: dict[str, dict[str, frozenset[str]]] = {
    # ``ruling_text_html`` is set on ``LASplitRuling`` by the deterministic
    # LA HTML splitter (#2450) and IS propagated through the worker's
    # ``_try_la_html_split`` split_event dict.  The reingest path
    # ``_full_reparse_document`` does not currently surface it — every LA
    # reingest after #2450 loses the per-case HTML.  Tracked separately
    # as a follow-up to #4298 (filed during retrospective).
    "LASplitRuling": {"reingest": frozenset({"ruling_text_html"})},
}


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
    target: str  # "worker" or "reingest"
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
                if isinstance(tgt, ast.Name) and tgt.id == "__slots__":
                    if isinstance(stmt.value, (ast.Tuple, ast.List)):
                        for elt in stmt.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
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
    bare_name_counts: dict[str, int] = {}

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
                bare_name_counts[bare] = bare_name_counts.get(bare, 0) + 1
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
# Discovery: reingest ``_full_reparse_document``
# ---------------------------------------------------------------------------


def _ruling_attrs_in_function(
    node: ast.FunctionDef, loop_var: str = "ruling"
) -> frozenset[str]:
    """Return the set of attribute names accessed on the ``ruling`` loop
    variable (or an alias, if the body assigns one) inside *node*.

    Recognizes:
      * ``ruling.<name>`` direct attribute access
      * ``getattr(ruling, "<name>", ...)`` calls

    Aliases of ``ruling`` are not tracked — the reingest path uses the
    canonical name everywhere today.
    """
    attrs: set[str] = set()
    for sub in ast.walk(node):
        # ruling.<attr>
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == loop_var
        ):
            attrs.add(sub.attr)

        # getattr(ruling, "<attr>", default)
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "getattr"
            and len(sub.args) >= 2
            and isinstance(sub.args[0], ast.Name)
            and sub.args[0].id == loop_var
            and isinstance(sub.args[1], ast.Constant)
            and isinstance(sub.args[1].value, str)
        ):
            attrs.add(sub.args[1].value)

    return frozenset(attrs)


def _extracted_dict_keys_in_function(node: ast.FunctionDef) -> frozenset[str]:
    """Walk *node* and return the union of literal string keys assigned
    to any ``extracted = {...}`` (or ``extracted: dict = {...}``) literal.
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

        if not any(isinstance(t, ast.Name) and t.id == "extracted" for t in targets):
            continue
        if not isinstance(value, ast.Dict):
            continue
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return frozenset(keys)


def discover_reingest_propagation(
    reingest_path: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``_full_reparse_document`` in *reingest_path* and return:

      * the set of literal keys in the function's ``extracted`` dict literal
      * the set of attribute names accessed on the ``ruling`` loop variable

    A field is considered "propagated" through reingest if it appears in
    EITHER set.  Returns two empty frozensets if the function is not
    found.
    """
    if not reingest_path.is_file():
        return (frozenset(), frozenset())

    tree = ast.parse(
        reingest_path.read_text(encoding="utf-8"), filename=str(reingest_path)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_full_reparse_document":
            return (
                _extracted_dict_keys_in_function(node),
                _ruling_attrs_in_function(node),
            )
    return (frozenset(), frozenset())


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
    reingest_extracted: frozenset[str],
    reingest_ruling_attrs: frozenset[str],
    worker_path: Path,
    reingest_path: Path,
) -> tuple[list[Violation], list[Violation]]:
    """Cross-reference dataclass fields against worker + reingest sets.

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

        non_internal = dc.fields - _INTERNAL_FIELDS

        # Worker check
        worker_fn = scope.get("worker_fn")
        if isinstance(worker_fn, str):
            keys = worker_fns.get(worker_fn, frozenset())
            wl = _whitelist("worker", dc.key)
            if not keys and worker_fn:
                # Function not found in worker.py at all — that's a hard
                # error.  Either the scope table is wrong or worker.py
                # is corrupt.
                blocking.append(
                    Violation(
                        dataclass_name=dc.class_name,
                        field="<function not found>",
                        target="worker",
                        function_name=worker_fn,
                        file_path=str(worker_path),
                    )
                )
            else:
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

        # Reingest check
        if scope.get("reingest", True):
            reingest_combined = reingest_extracted | reingest_ruling_attrs
            wl = _whitelist("reingest", dc.key)
            for f in sorted(non_internal - reingest_combined):
                v = Violation(
                    dataclass_name=dc.class_name,
                    field=f,
                    target="reingest",
                    function_name="_full_reparse_document",
                    file_path=str(reingest_path),
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


def _default_reingest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "reingest_from_s3.py"


def _default_worker_path() -> Path:
    return _default_scraper_framework_root() / "ingestion" / "worker.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every *SplitRuling dataclass field is propagated "
            "through the worker + reingest split paths (issue #4298)."
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
        "--reingest",
        type=Path,
        default=_default_reingest_path(),
        help="Path to scripts/reingest_from_s3.py (default: repo root).",
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
    reingest_path: Path = args.reingest

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
    reingest_extracted, reingest_ruling_attrs = discover_reingest_propagation(
        reingest_path
    )

    blocking, whitelisted = cross_check(
        dataclasses,
        worker_fns,
        reingest_extracted,
        reingest_ruling_attrs,
        worker_path,
        reingest_path,
    )

    if whitelisted and not args.quiet_whitelisted:
        for v in whitelisted:
            print(
                f"  (whitelisted) {v.render()}",
                file=sys.stderr,
            )

    if blocking:
        for v in blocking:
            print(v.render())
        print(
            f"\nFound {len(blocking)} *SplitRuling propagation gap(s).  "
            "Either propagate the field through the worker / reingest path, "
            "or add it to _KNOWN_PROPAGATION_GAPS in "
            "scripts/check_split_ruling_fields_propagated.py with a "
            "tracking issue.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
