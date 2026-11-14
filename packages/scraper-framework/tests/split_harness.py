"""Invariant harness for split / dedup / supersede / relink (#4845).

Drives random sequences of split results for one S3 key through the real
write paths against a real Postgres, and checks after **every commit** the
properties that must hold for any sequence of re-splits:

- ``gap``: a ruling that exists before and after a step is never missing at
  a commit in between (no ruling-less interval);
- ``alert``: a ``public.alert_events`` row keeps its ``ruling_id`` while its
  ruling still exists;
- ``superseded``: no superseded document holds a ruling, and there is no
  ``previous_version_id`` cycle (``scripts/audit_supersede_integrity.py``);
- ``search``: every document with a ruling has exactly one search doc, with
  that ruling's text, and no search doc exists without a ruling;
- ``search_fields``: every search doc the worker wrote still carries the
  metadata (judge name, case title, hearing date, ...) of the doc built
  from ``derived.*`` now, for every key the harness has written, not only
  the one the step touched.  A judge rename made by one key's write must
  re-index the judge's rulings on other keys (#4920), and so must a case
  retitle (:func:`run_shared_case_retitle`, #4935);
- ``date_source``: a ruling with a ``hearing_date`` has a
  ``hearing_date_source``;
- ``synthetic_title``: a ruling on its own synthetic ``UNKNOWN-<document id>``
  case carries that ruling's own caption, never a previous slot occupant's;
- ``converge``: re-ingesting the final bytes leaves the same rows as a fresh
  ingest of those bytes, and a fresh live capture and a fresh reingest of the
  same bytes leave the same rows;
- ``kept``: a split child whose per-child LLM enrichment is exhausted keeps
  the document and ruling its slot already held (#4878).  Checked by
  :func:`run_enrichment_failure`, which drives each deterministic splitter.
- ``telemetry``: a ``telemetry.validation_results`` row for the key, once
  committed, is never deleted, including a fail row the same step logged
  for a child it then dropped as stale (#4879).  ``telemetry.*`` is not
  rebuildable from S3.
- ``evidence``: after a step whose document text can settle a case number
  (``MIN_DOCUMENT_TEXT_CHARS`` or more), every ruling on the key is filed
  under a number that text prints, or under its ``UNKNOWN-`` case (#4907,
  #4915, #4927).  A live step keeps an established case link
  (preserve-first, #2475), so on a live step a ruling that kept its row and
  its case is exempt.  An LLM's misread or invented number, whichever
  extractor (the LLM split, the regex fallback over the LLM's
  transcription, the fuzzy correction) produced it, breaks it.
- ``judge_converge``: a court's rulings of one roster judge, whatever
  spelling their documents print, resolve to one judge row; a judge is
  renamed only to a spelling at least two documents print, so one typo
  document never renames (#4928); and when the documents decide a judge's
  name (``_choose_canonical_spelling`` returns it whatever the current
  name), ingesting the same document in the opposite row order ends on
  the same name (#4922).
- ``untouched``: a step whose extraction fails (every page's and chunk's
  API call failed, ``ExtractionIncompleteError``) leaves the key's rows and
  search docs exactly as they were: no whole-document ruling next to the
  key's split children, no deletions (#4948).  A split-set replacement
  (``reingest`` / ``db``) reports the key as failed
  (``SplitExtractionFailedError``), so the reingest's failed-keys manifest
  retries it.  Checked by :func:`run_extraction_failure`.

A *ruling* is identified by its text and case number (a synthetic
``UNKNOWN-*`` case counts as one case number, ``UNKNOWN``), so a ruling that
moves between split slots, or is relinked to a better case, is still tracked.

The extractor is stubbed: each step decides the rows the multimodal
extractor returns for the key's PDF.  Everything downstream of it (the
worker's split dispatch, validation, the case-number gate, the
case/document/ruling writes, judge resolution and arbitration, the split-set
write, content-hash dedup, stale-child cleanup, search indexing) is the
production code.

The generator models what the gate and judge resolution read (#4929): the
document's extracted text (the parent event's ``ruling_text``: a text layer
printing each row's real case number, a scanned PDF's few characters, or
nothing), the LLM's transcription of each ruling (which can repeat a
misread), the case number the LLM reads (the printed one, a one- or
two-character misread, or an invented one such as the prompt example
``2025-00912345``), the judge name each ruling prints (a roster judge's
usual spelling, the roster spelling, an extra initial, or a one-letter
typo) and the court's roster.  Some steps are whole-document steps: the
extractor finds no rulings and the worker writes the document as one ruling
(the #4915 whole-document move).

Step kinds:

- ``live``: a scraper capture event (a structured hearing date, no split-set
  replacement);
- ``reingest``: a prefix-reingest / rebuild event (``_replace_split_set``);
- ``db``: ``scripts/reingest_from_s3.py`` in DB-row mode, run through
  :data:`DB_MODE_DRIVER`.

Failures raise :class:`InvariantError` naming the invariant, the seed
and the step, so a failing seed is its own reproduction:
``SPLIT_HARNESS_SEED=<n> pytest tests/test_split_invariants_pg.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg

from framework.llm_schema import ExtractedParty, ExtractedRuling
from framework.search.indexer import search_doc_metadata
from framework.search.ruling_doc import load_search_events
from ingestion.db import (
    _choose_canonical_spelling,
    _normalize_for_comparison,
    normalize_judge_name,
)
from ingestion.extract import case_number_supported_by_text
from ingestion.split_ids import derive_parent_document_id, make_split_document_id

try:
    from ingestion.case_evidence import MIN_DOCUMENT_TEXT_CHARS
except ImportError:  # trees before #4927, so a recorded seed can be bisected
    MIN_DOCUMENT_TEXT_CHARS = 200

CAPTURED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HEARING = date(2026, 9, 4)

#: Distinct first names / defendants for generated captions.
_PLAINTIFFS = ["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Ginkgo", "Hazel"]
_DEFENDANTS = ["Acme Corp", "Beta LLC", "Crane Inc", "Delta Co", "Echo Ltd", "Fox LP"]

INVARIANTS = (
    "gap",
    "alert",
    "superseded",
    "search",
    "search_fields",
    "date_source",
    "synthetic_title",
    "converge",
    "kept",
    "telemetry",
    "evidence",
    "judge_converge",
    "untouched",
    "error",
)


#: ``ingestion.worker.SPLIT_EXTRACTION_FAILED_EVENT`` (#4948).
_EXTRACTION_FAILED_EVENT = "llm_split.extraction_failed"


class InvariantError(AssertionError):
    """An invariant failed.  ``invariant`` is one of :data:`INVARIANTS`."""

    def __init__(self, invariant: str, message: str) -> None:
        super().__init__(f"[{invariant}] {message}")
        self.invariant = invariant


# ---------------------------------------------------------------------------
# Model: the rulings a court posted, and the rows one extraction returns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One ruling the court posted.  ``text`` is unique per key."""

    idx: int
    case_number: str | None
    title: str | None
    text: str
    #: The judge name the extraction reads for the ruling (#4920).
    judge: str | None = None
    #: The case number the document's text prints for the ruling (#4929),
    #: when it differs from ``case_number`` (what the LLM reads: a misread,
    #: an invented number, or none).  ``None``: the text prints
    #: ``case_number``.
    printed: str | None = None

    @property
    def printed_number(self) -> str | None:
        return self.printed or self.case_number


@dataclass(frozen=True)
class Row:
    """One row an extraction returns for the PDF.

    ``kind`` is ``"ruling"`` (the spec's ruling), ``"textless"`` (a fused
    tail or empty tentative cell carrying the spec's caption and no text) or
    ``"preamble"`` (a textless calendar header row).
    """

    kind: str
    spec: int = -1


@dataclass(frozen=True)
class Step:
    kind: str  # live | reingest | db
    rows: tuple[Row, ...]
    specs: tuple[Spec, ...]
    ops: tuple[str, ...] = ()
    #: The document's extracted text: the parent event's ``ruling_text``
    #: (#4929).  ``""`` is no text at all; a scanned PDF has a few
    #: characters, below ``MIN_DOCUMENT_TEXT_CHARS``.
    doc: str = ""
    #: The extractor finds no rulings, so the worker writes the whole
    #: document as one ruling (``doc`` is its text).
    whole: bool = False
    #: The court's roster (``court_directory_snapshots``) spellings.
    roster: tuple[str, ...] = ()
    #: Tag on the extracted parties' names (none when empty), unique per
    #: seed: see :func:`_parties`.
    party_tag: str = ""
    #: Every extraction call fails (``ExtractionIncompleteError``, #4948).
    fail: bool = False

    def describe(self) -> str:
        parts = []
        for row in self.rows:
            if row.kind == "preamble":
                parts.append("pre")
            else:
                spec = self.specs[row.spec]
                tag = f"R{spec.idx}" if row.kind == "ruling" else f"t{spec.idx}"
                if spec.case_number is None:
                    tag += "?"
                elif spec.case_number != spec.printed_number:
                    tag += "!"  # the LLM's number is not the printed one
                if spec.title is None:
                    tag += "~"
                if row.kind == "ruling" and spec.judge:
                    tag += f"@{spec.judge}"
                parts.append(tag)
        doc = "none" if not self.doc else "scan" if not doc_is_evidence(self.doc) else "text"
        whole = " whole" if self.whole else ""
        fail = " FAIL" if self.fail else ""
        return f"{self.kind}[{' '.join(parts)}] doc={doc}{whole}{fail} ops={list(self.ops)}"


def doc_is_evidence(doc: str | None) -> bool:
    """Whether a document's text is long enough to settle a case number."""
    return len((doc or "").strip()) >= MIN_DOCUMENT_TEXT_CHARS


def _case_number(idx: int) -> str:
    # Five repeated digits: every pair is Levenshtein 5 apart, beyond the
    # enrichment fuzzy matcher's reach (distance 2).
    return f"30-2026-{str(idx % 10) * 5}"


#: Case-number shapes for counties whose format validation checks
#: (``case_number_matches_county_format``, #4907); repeated digits keep every
#: pair beyond the fuzzy matcher's reach.
_COUNTY_CASE_NUMBERS: dict[str, Callable[[int], str]] = {
    "Santa Clara": lambda idx: f"26CV{str(idx % 10) * 6}",
    "San Francisco": lambda idx: f"CGC-26-{str(idx % 10) * 6}",
}


def _county_case_number(county: str, idx: int) -> str:
    return _COUNTY_CASE_NUMBERS.get(county, _case_number)(idx)


def _title(idx: int, variant: int = 0) -> str:
    plaintiff = _PLAINTIFFS[idx % len(_PLAINTIFFS)]
    defendant = _DEFENDANTS[(idx + variant) % len(_DEFENDANTS)]
    return f"{plaintiff} v. {defendant}"


def _text(token: str, idx: int) -> str:
    return (
        f"Ruling {token}-{idx}. The motion is GRANTED as to cause of action {idx} "
        "only; the remaining causes of action stand. Moving party to give notice."
    )


_OPS = (
    "insert_textless",
    "insert_preamble",
    "drop_row",
    "drop_first",
    "insert_ruling",
    "swap",
    "duplicate",
    "single",
    "title_none",
    "title_set",
    "case_none",
    "case_set",
    "same",
)


def generate(seed: int, *, whole_documents: bool | None = None) -> list[Step]:
    """Return the step sequence for *seed* (deterministic).

    The rows, ops and step kinds come from one random stream, as they did
    before #4929, so a seed's split sequence is unchanged.  The document
    text, the case number the LLM reads, the judge names and the roster
    come from a second stream (:class:`_Model`).  *whole_documents*
    (default :data:`WHOLE_DOCUMENT_STEPS`) turns the ``whole`` op on; the
    random streams are the same either way.
    """
    rng = random.Random(seed)
    model = _Model(seed, WHOLE_DOCUMENT_STEPS if whole_documents is None else whole_documents)
    token = f"s{seed}"
    n_specs = rng.randint(3, 7)
    specs = []
    for i in range(n_specs):
        case_number = None if rng.random() < 0.2 else model.shape(i)
        title = None if rng.random() < 0.3 else _title(i)
        specs.append(
            Spec(
                idx=i,
                case_number=case_number,
                title=title,
                text=_text(token, i),
                judge=model.first_judge(),
                printed=model.shape(i),
            )
        )
    order = list(range(n_specs))
    rng.shuffle(order)
    first = sorted(order[: rng.randint(1, n_specs)])
    rows = [Row("ruling", i) for i in first]
    steps = [model.step(rng.choice(("live", "reingest", "db")), rows, specs, [], token)]

    for _ in range(rng.randint(2, 6)):
        ops = [rng.choice(_OPS) for _ in range(rng.randint(1, 2))]
        for op in ops:
            rows, specs = _apply_op(rng, op, rows, specs, model.shape)
        kind = rng.choices(("live", "reingest", "db"), weights=(3, 4, 2))[0]
        steps.append(model.step(kind, rows, specs, ops, token))
    return steps


# ---------------------------------------------------------------------------
# What the case-number gate and judge resolution read (#4929)
# ---------------------------------------------------------------------------

#: Case-number shapes a seed's court prints.  Each repeats one digit, so
#: every pair is beyond the fuzzy matcher's reach (distance 2).  The
#: regex fallback (``extract_case_number``) reads the Riverside and Orange
#: shapes from a ruling's text (Orange as ``2026-NNNNNNNN``, the way it
#: reads real Orange numbers); it does not read the harness shape.
_SHAPES: dict[str, Callable[[int], str]] = {
    "harness": _case_number,
    "riverside": lambda idx: f"CVRI25{str(idx % 10) * 5}",
    "orange": lambda idx: f"30-2026-{str(idx % 10) * 8}",
}

#: Numbers an LLM invents: the extraction prompt's own example (#4907) and
#: other counties' shapes.  None is printed by any generated document.
INVENTED_CASE_NUMBERS = ("2025-00912345", "23STCV01234", "CGC-25-612345", "24CV998877")


@dataclass(frozen=True)
class RosterJudge:
    """One judge on the court's roster and the spellings documents print."""

    roster: str  # the court directory's spelling
    printed: str  # how most calendars print it
    initial: str  # with an extra middle initial
    typo: str  # a one-letter typo of the surname

    def spellings(self) -> tuple[str, ...]:
        return (self.roster, self.printed, self.initial, self.typo)


#: Every typo is one letter from the roster spelling (``fuzzy_match_roster_name``
#: confidence 0.85, the threshold), and every spelling of one judge is
#: beyond the roster match of the others.
ROSTER_JUDGES = (
    RosterJudge("Craig L. Griffin", "Craig Griffin", "Craig L. M. Griffin", "Craig L. Grifin"),
    RosterJudge(
        "Rafael A. Sivilla-Jones",
        "Rafael Sivilla-Jones",
        "Rafael A. B. Sivilla-Jones",
        "Rafael A. Sivila-Jones",
    ),
    RosterJudge("H. Shaina Colover", "Shaina Colover", "Shaina H. Colover", "H. Shaina Colovr"),
)

_JUDGE_OF_SPELLING: dict[str, RosterJudge] = {
    s.lower(): j for j in ROSTER_JUDGES for s in j.spellings()
}


def roster_judge(spelling: str | None) -> RosterJudge | None:
    """The roster judge a generated spelling belongs to."""
    return _JUDGE_OF_SPELLING.get((spelling or "").lower())


#: Ops on what the LLM reads and what the document prints.
_MODEL_OPS = (
    "case_misread",  # the LLM reads one or two characters off the printed number
    "case_misread_text",  # ... and its transcription of the ruling repeats the misread
    "case_invented",  # the LLM invents a number no document prints
    "case_invented_text",  # ... and its transcription repeats it
    "case_cited",  # the transcription cites the printed number
    "case_fix",  # the LLM reads the printed number again
    "whole",  # the extractor finds no rulings: the document is one ruling
)
_JUDGE_OPS = ("judge_typo", "judge_initial", "judge_roster", "judge_printed")

#: Whether generated seeds include ``whole`` steps.  On since #4940: a
#: whole document is the one child of a split set, written by the same
#: split-set write as any re-split, so it replaces the key's earlier
#: children, leaves a placeholder case for a real number, and a rejected
#: document leaves the key with no rows.  ``test_known_bug`` pins the seeds
#: that broke before the fix.
WHOLE_DOCUMENT_STEPS = True

_DOC_FILLER = (
    "The court has read and considered the moving, opposition and reply papers. "
    "Any party who wishes to be heard must give notice by telephone. "
)


class _Model:
    """The second random stream of a seed: document text, the numbers the
    LLM reads, judge names and the roster."""

    def __init__(self, seed: int, whole_documents: bool) -> None:
        self.rng = random.Random(f"{seed}:model")
        self.whole_documents = whole_documents
        self.shape = _SHAPES[self.rng.choice(sorted(_SHAPES))]
        n_judges = self.rng.choices((0, 1, 2), weights=(4, 5, 1))[0]
        self.judges = tuple(self.rng.sample(ROSTER_JUDGES, n_judges))

    def first_judge(self) -> str | None:
        if not self.judges:
            return None
        judge = self.rng.choice(self.judges)
        return judge.printed if self.rng.random() < 0.75 else self.rng.choice(judge.spellings())

    def step(
        self, kind: str, rows: list[Row], specs: list[Spec], ops: list[str], token: str
    ) -> Step:
        """Build a step from the split stream's rows and specs, applying
        this stream's ops to *specs* in place (a misread persists)."""
        ops = list(ops)
        whole = False
        pool = _MODEL_OPS + (_JUDGE_OPS if self.judges else ())
        for _ in range(self.rng.choice((0, 1, 1, 2))):
            op = self.rng.choice(pool)
            if op == "whole":
                if not self.whole_documents:
                    continue
                whole = True
            else:
                self._apply(op, specs, rows, token)
            ops.append(op)
        mode = self.rng.choices(("text", "scan", "none"), weights=(6, 2, 2))[0]
        if whole and mode == "none":
            mode = "text"
        doc = document_text(rows, specs, mode)
        return Step(
            kind,
            tuple(rows),
            tuple(specs),
            tuple(ops),
            doc=doc,
            whole=whole,
            roster=tuple(j.roster for j in self.judges),
            party_tag=token,
        )

    def _apply(self, op: str, specs: list[Spec], rows: list[Row], token: str) -> None:
        on_rows = sorted({r.spec for r in rows if r.kind == "ruling"})
        i = self.rng.choice(on_rows) if on_rows else self.rng.randrange(len(specs))
        spec = specs[i]
        printed = spec.printed_number or self.shape(spec.idx)
        base = _text(token, spec.idx)
        if op.startswith("case_misread"):
            number = misread(printed, self.rng)
        elif op.startswith("case_invented"):
            number = self.rng.choice(INVENTED_CASE_NUMBERS)
        if op in ("case_misread", "case_invented"):
            spec = replace(spec, case_number=number, printed=printed)
        elif op in ("case_misread_text", "case_invented_text"):
            spec = replace(spec, case_number=number, printed=printed, text=_cites(base, number))
        elif op == "case_cited":
            spec = replace(spec, printed=printed, text=_cites(base, printed))
        elif op == "case_fix":
            spec = replace(spec, case_number=printed, printed=printed, text=base)
        elif op.startswith("judge_"):
            judge = roster_judge(spec.judge) or self.rng.choice(self.judges)
            spelling = getattr(judge, op.removeprefix("judge_"))
            spec = replace(spec, judge=spelling)
        specs[i] = spec


def _cites(text: str, number: str) -> str:
    """A transcription that names *number* (the LLM's or the printed one)."""
    return f"{text} The motion in case {number} is decided as stated."


def misread(printed: str, rng: random.Random) -> str:
    """*printed* with one or two of its digits misread (within the fuzzy
    matcher's distance-2 reach of the printed number)."""
    digits = [m.start() for m in re.finditer(r"\d", printed)]
    tail = digits[-5:]  # the serial, not the year or the court prefix
    chars = list(printed)
    for pos in rng.sample(tail, rng.choice((1, 1, 2))):
        chars[pos] = rng.choice([d for d in "0123456789" if d != chars[pos]])
    return "".join(chars)


def document_text(
    rows: list[Row] | tuple[Row, ...], specs: list[Spec] | tuple[Spec, ...], mode: str
) -> str:
    """The extracted text of a calendar PDF listing *rows*.

    ``text``: a text layer printing each listed ruling's real case number
    and caption (at least ``MIN_DOCUMENT_TEXT_CHARS``); ``scan``: a scanned
    PDF whose text layer is a few characters; ``none``: no text.
    """
    if mode == "none":
        return ""
    if mode == "scan":
        return "Tentative rulings (scanned image)"
    lines = ["SUPERIOR COURT OF CALIFORNIA. Tentative rulings for the civil calendar."]
    listed: list[int] = []
    for row in rows:
        if row.kind == "preamble" or row.spec in listed:
            continue
        listed.append(row.spec)
        spec = specs[row.spec]
        number = spec.printed_number or "pending assignment"
        lines.append(f"Item. Case No. {number} {spec.title or 'Untitled matter'}.")
    lines.append(_DOC_FILLER * 2)
    return " ".join(lines)


def _parties(title: str | None, tag: str) -> list[ExtractedParty]:
    """The parties of a generated caption (``Plaintiff v. Defendant``).

    Party rows are global (not per court), so each seed's parties carry
    the seed's *tag*: concurrent seeds then never wait on each other's
    party rows, and a seed's party history is its own.
    """
    if not tag or not title or " v. " not in title:
        return []
    plaintiff, defendant = title.split(" v. ", 1)
    return [
        ExtractedParty(name=f"{plaintiff} {tag}", role="plaintiff"),
        ExtractedParty(name=f"{defendant} {tag}", role="defendant"),
    ]


def _apply_op(
    rng: random.Random,
    op: str,
    rows: list[Row],
    specs: list[Spec],
    shape: Callable[[int], str] = _case_number,
) -> tuple[list[Row], list[Spec]]:
    rows = list(rows)
    specs = list(specs)
    used = {r.spec for r in rows if r.kind == "ruling"}
    if op == "insert_textless":
        rows.insert(rng.randint(0, len(rows)), Row("textless", rng.randrange(len(specs))))
    elif op == "insert_preamble":
        rows.insert(0, Row("preamble"))
    elif op == "drop_row" and len(rows) > 1:
        rows.pop(rng.randrange(len(rows)))
    elif op == "drop_first" and len(rows) > 1:
        rows.pop(0)
    elif op == "insert_ruling":
        unused = [s.idx for s in specs if s.idx not in used]
        if unused:
            rows.insert(rng.randint(0, len(rows)), Row("ruling", rng.choice(unused)))
    elif op == "swap" and len(rows) > 1:
        i = rng.randrange(len(rows) - 1)
        rows[i], rows[i + 1] = rows[i + 1], rows[i]
    elif op == "duplicate":
        candidates = [i for i, r in enumerate(rows) if r.kind == "ruling"]
        if candidates:
            i = rng.choice(candidates)
            rows.insert(i + 1, rows[i])
    elif op == "single":
        candidates = [r for r in rows if r.kind == "ruling"]
        if candidates:
            rows = [rng.choice(candidates)]
    elif op in ("title_none", "title_set", "case_none", "case_set"):
        i = rng.randrange(len(specs))
        spec = specs[i]
        if op == "title_none":
            spec = replace(spec, title=None)
        elif op == "title_set":
            spec = replace(spec, title=_title(spec.idx, rng.randint(1, 5)))
        elif op == "case_none":
            spec = replace(spec, case_number=None)
        else:
            spec = replace(spec, case_number=spec.printed or shape(spec.idx))
        specs[i] = spec
    return rows, specs


def extracted_rulings(step: Step) -> list[ExtractedRuling]:
    """The multimodal extractor's output for *step*."""
    out: list[ExtractedRuling] = []
    if step.whole:
        return out
    for n, row in enumerate(step.rows):
        if row.kind == "preamble":
            out.append(ExtractedRuling(ruling_text="", entry_number=None))
            continue
        spec = step.specs[row.spec]
        is_ruling = row.kind == "ruling"
        out.append(
            ExtractedRuling(
                extracted_case_number=spec.case_number,
                extracted_case_title=spec.title,
                extracted_parties=_parties(spec.title, step.party_tag) if is_ruling else [],
                extracted_judge_name=spec.judge if is_ruling else None,
                ruling_text=spec.text if is_ruling else "",
                hearing_date=HEARING.isoformat(),
                motion_type="demurrer" if is_ruling else None,
                entry_number=n + 1 if is_ruling else None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# State readout
# ---------------------------------------------------------------------------


@dataclass
class DocRow:
    id: str
    status: str
    previous_version_id: str | None
    ruling_id: str | None
    text: str | None
    case_number: str | None
    case_title: str | None
    hearing_date: date | None
    hearing_date_source: str | None


def _norm_case(case_number: str | None) -> str:
    if not case_number or case_number.startswith("UNKNOWN-"):
        return "UNKNOWN"
    return case_number


def read_key(conn: psycopg.Connection, s3_key: str) -> list[DocRow]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.id::text, d.status::text, d.previous_version_id::text, r.id::text, "
            "r.ruling_text, c.case_number, c.case_title, r.hearing_date, "
            "r.hearing_date_source "
            "FROM documents d LEFT JOIN rulings r ON r.document_id = d.id "
            "LEFT JOIN cases c ON c.id = r.case_id WHERE d.s3_key = %s ORDER BY d.id",
            (s3_key,),
        )
        return [DocRow(*row) for row in cur.fetchall()]


def ruling_identities(rows: list[DocRow]) -> Counter[tuple[str, str]]:
    return Counter((r.text, _norm_case(r.case_number)) for r in rows if r.ruling_id)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubIndexer:
    """Records the search index as ``{document id: indexed ruling text}``,
    and the indexed metadata of each doc as ``meta``."""

    def __init__(self) -> None:
        self.docs: dict[str, str | None] = {}
        self.meta: dict[str, dict[str, Any]] = {}

    def index_document(self, event: dict[str, Any], *, force: bool = False) -> bool:
        doc_id = str(event["document_id"])
        self.docs[doc_id] = event.get("ruling_text")
        self.meta[doc_id] = search_doc_metadata(event)
        return True

    def index_batch(self, events: list[dict[str, Any]], *, force: bool = False) -> int:
        for event in events:
            self.index_document(event, force=force)
        return len(events)

    def delete_documents(self, document_ids: list[str]) -> int:
        for doc_id in document_ids:
            self.docs.pop(str(doc_id), None)
            self.meta.pop(str(doc_id), None)
        return len(document_ids)


class FakeExtractor:
    """Extractor double (multimodal and text): returns the current step's
    rows, or raises ``ExtractionIncompleteError`` when ``failing``, as a
    strict extraction whose API calls all failed does (#4940, #4948)."""

    def __init__(self) -> None:
        self.rulings: list[ExtractedRuling] = []
        self.failing = False

    def _result(self) -> list[ExtractedRuling]:
        if self.failing:
            from framework.llm_extractor import ExtractionIncompleteError

            raise ExtractionIncompleteError("harness: every API call failed")
        return [r.model_copy() for r in self.rulings]

    def extract_from_pdf(self, _pdf: bytes, **_kwargs: Any) -> list[ExtractedRuling]:
        return self._result()

    def extract(self, _text: str, **_kwargs: Any) -> list[ExtractedRuling]:
        return self._result()


@contextmanager
def _real_enrichment() -> Iterator[None]:
    """Run the worker with the real ``EnrichmentEngine``.

    The suite's ``conftest`` replaces it with a no-op for the unit tests,
    which hid the enrichment step from the harness: its fuzzy case-number
    correction (and the gate's check of it, #4927) and its judge alias
    resolution never ran (#4929).
    """
    from framework.enrichment import EnrichmentEngine
    from ingestion import worker as worker_mod

    with patch.object(worker_mod, "EnrichmentEngine", EnrichmentEngine):
        yield


class _TelemetryHandler(logging.Handler):
    """Counts the ``telemetry_event`` of every log record (#4929), so a
    test can tell which code paths a seed reached."""

    def __init__(self, counter: Counter[str]) -> None:
        super().__init__(level=logging.INFO)
        self.counter = counter

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "telemetry_event", None)
        if isinstance(event, str):
            self.counter[event] += 1


def _spelled(spelling: str | None) -> str:
    """The lowercased spelling ``resolve_judge`` stores for *spelling*."""
    return (normalize_judge_name(spelling) or "").lower() if spelling else ""


def _family(spelling: str) -> str:
    """The family of spellings *spelling* belongs to (initials, case and
    word order aside), as arbitration groups them."""
    return _normalize_for_comparison(spelling) if spelling else ""


class _CheckedConn:
    """A connection proxy that runs *on_commit* after every commit."""

    def __init__(self, real: psycopg.Connection, on_commit: Callable[[], None]) -> None:
        self._real = real
        self._on_commit = on_commit

    def commit(self) -> None:
        self._real.commit()
        self._on_commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# DB-row reingest driver
# ---------------------------------------------------------------------------

#: How a ``db`` step runs ``scripts/reingest_from_s3.py`` in DB-row mode.
#: Called with ``(harness, key, step)``; the harness sets it up in
#: :func:`default_db_mode_driver`.
DbModeDriver = Callable[["KeyHarness", "Key", Step], None]


def default_db_mode_driver(harness: KeyHarness, key: Key, step: Step) -> None:
    """Run ``scripts/reingest_from_s3.py`` DB-row mode over *key*.

    The script's own key selection (``select_db_keys``) and event builder
    (``_build_prefix_event``) run unchanged; each selected object goes
    through the harness worker instead of a process pool, with the S3 read
    and the extractor stubbed.
    """
    import reingest_from_s3 as reingest

    filters, params = reingest._build_filters(None, None, None, s3_key_list=[key.s3_key])
    for s3_key in reingest.select_db_keys(harness.checker, filters, params):
        event = reingest._build_prefix_event(
            s3_key,
            b"",
            reingest._parse_s3_key(s3_key),
            "harness-bucket",
            capture_timestamp=CAPTURED_AT,
        )
        if step.doc:
            # The text layer the worker would extract from the PDF bytes.
            event["ruling_text"] = step.doc
        harness.worker.process_event(event)


DB_MODE_DRIVER: DbModeDriver = default_db_mode_driver


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@dataclass
class Key:
    s3_key: str
    content_hash: str
    parent: str
    county: str
    slots: dict[str, str] = field(default_factory=dict)

    def label(self, doc_id: str) -> str:
        if doc_id == self.parent:
            return "P"
        return self.slots.get(doc_id, f"X:{doc_id[:8]}")


def make_key(tag: str, county_tag: str | None = None) -> Key:
    content_hash = hashlib.sha256(tag.encode()).hexdigest()
    parent = derive_parent_document_id(content_hash)
    # Each key gets its own county (so its own court and case rows); the
    # slug is in the key, where the reingest script reads the county from.
    # Keys made with the same *county_tag* share a county, so their rulings
    # share a court and its judges (#4920).
    county_seed = county_tag or tag
    county_slug = f"harness_{hashlib.sha1(county_seed.encode()).hexdigest()[:10]}"
    return Key(
        s3_key=f"ca/{county_slug}/superior_court/raw/{content_hash}.pdf",
        content_hash=content_hash,
        parent=parent,
        county=county_slug.replace("_", " ").title(),
        slots={make_split_document_id(parent, i): f"S{i}" for i in range(64)},
    )


class KeyHarness:
    """Runs steps for keys against *dsn* and checks the invariants."""

    def __init__(
        self, dsn: str, run_token: str, seed: int, invariants: frozenset[str] | None = None
    ) -> None:
        self.dsn = dsn
        #: The invariants this run enforces (all by default); ``error`` is
        #: always enforced.
        self.invariants = (invariants or frozenset(INVARIANTS)) | {"error"}
        self.seed = seed
        self.run_token = run_token
        self.checker = psycopg.connect(dsn, autocommit=True)
        self.real = psycopg.connect(dsn, autocommit=False)
        self.indexer = StubIndexer()
        self.extractor = FakeExtractor()
        self._key: Key | None = None
        self._step_label = ""
        self._snapshots: list[Counter[tuple[str, str]]] = []
        #: ``validation_results`` ids for the key seen committed this step.
        self._validation_seen: set[str] = set()
        self._pending: InvariantError | None = None
        self.conn = _CheckedConn(self.real, self._after_commit)
        #: alert id -> (s3_key, ruling identity)
        self._alerts: dict[str, tuple[str, tuple[str, str]]] = {}
        self._user_sub: str | None = None
        #: Fields merged into every step's event (a deterministic splitter's
        #: county, format and text).
        self.event_extra: dict[str, Any] = {}
        #: ``telemetry_event`` values the code under test logged (#4929).
        self.events: Counter[str] = Counter()
        self._log_handler = _TelemetryHandler(self.events)
        self._ingestion_logger = logging.getLogger("ingestion")
        self._saved_log_level = self._ingestion_logger.level
        self._ingestion_logger.addHandler(self._log_handler)
        if self._ingestion_logger.getEffectiveLevel() > logging.INFO:
            self._ingestion_logger.setLevel(logging.INFO)
        #: Keys whose court has its roster snapshot.
        self._rostered: set[str] = set()
        #: The judge spelling each ruling text printed, as of the last step
        #: that wrote it (ruling texts are unique per key).
        self._spelling: dict[str, str | None] = {}
        self.worker = self._make_worker()

    def close(self) -> None:
        self._ingestion_logger.removeHandler(self._log_handler)
        self._ingestion_logger.setLevel(self._saved_log_level)
        self.real.close()
        self.checker.close()

    # -- worker ---------------------------------------------------------

    def _make_worker(self) -> Any:
        from ingestion import worker as worker_mod

        with patch.object(worker_mod, "create_llm_client", return_value=None):
            worker = worker_mod.IngestionWorker(
                redis_client=MagicMock(),
                pg_dsn=self.dsn,
                opensearch_client=MagicMock(),
                s3_client=MagicMock(),
                archive_bucket="harness-bucket",
            )
        worker._conn = self.real
        worker._get_connection = lambda: self.conn  # type: ignore[method-assign]
        worker._indexer = self.indexer
        worker._fetch_raw_pdf_from_s3 = lambda *_a, **_k: b"%PDF-1.4 harness"  # type: ignore[method-assign]
        worker._get_multimodal_extractor = lambda *_a, **_k: self.extractor  # type: ignore[method-assign]
        # The text fallback a failed multimodal extraction tries (#4948).
        worker._get_framework_extractor = lambda *_a, **_k: self.extractor  # type: ignore[method-assign]
        worker._get_county_extractor = lambda *_a, **_k: self.extractor  # type: ignore[method-assign]
        worker._validation_enabled = False
        worker._formatting_enabled = False
        worker._summarization_enabled = False
        return worker

    def _event(self, key: Key, kind: str, doc: str = "") -> dict[str, Any]:
        event: dict[str, Any] = {
            "document_id": key.parent,
            "state": "CA",
            "county": key.county,
            "court": "Superior Court",
            "content_format": "pdf",
            "content_hash": key.content_hash,
            "s3_key": key.s3_key,
            "s3_bucket": "harness-bucket",
            "source_url": "",
            "capture_timestamp": CAPTURED_AT.isoformat(),
            "ruling_text": doc,
        }
        if kind == "live":
            event["scraper_id"] = "ca-harness-tentatives"
            event["hearing_date"] = HEARING.isoformat()
        else:
            event["scraper_id"] = "reingest-ca-harness"
            event["_replace_split_set"] = True
        event.update(self.event_extra)
        return event

    # -- running --------------------------------------------------------

    def run_step(self, key: Key, step: Step, label: str) -> None:
        """Run one step on *key*; raise :class:`InvariantError` on a
        broken invariant."""
        self._key = key
        self._step_label = f"seed={self.seed} {label} {step.describe()}"
        self._attach_alerts(key)
        self._ensure_roster(key, step.roster)
        before_rows = read_key(self.checker, key.s3_key)
        before = ruling_identities(before_rows)
        judges_before = self._judge_rows(key)
        spelling_before = dict(self._spelling)
        search_before = self._search_docs(key)
        for row in step.rows:
            if row.kind == "ruling" and not step.whole and not step.fail:
                spec = step.specs[row.spec]
                self._spelling[spec.text] = spec.judge
        self._snapshots = []
        self._validation_seen = self._validation_ids(key)
        self._pending = None
        self.extractor.rulings = extracted_rulings(step)
        self.extractor.failing = step.fail
        # A failed extraction on a split-set replacement reports the key as
        # failed (#4948); a live capture does not raise.
        expect_failed = step.fail and step.kind != "live"
        reported_failed = False
        logged_before = self.events[_EXTRACTION_FAILED_EVENT]
        try:
            with _real_enrichment():
                if step.kind == "db":
                    DB_MODE_DRIVER(self, key, step)
                else:
                    self.worker.process_event(self._event(key, step.kind, step.doc))
        except InvariantError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a violation
            if not (expect_failed and type(exc).__name__ == "SplitExtractionFailedError"):
                self._fail("error", f"step raised {type(exc).__name__}: {exc}")
            reported_failed = True
        finally:
            self.extractor.failing = False
            try:
                self.real.rollback()
            except Exception:  # noqa: BLE001
                pass
        if self._pending is not None:
            raise self._pending
        after_rows = read_key(self.checker, key.s3_key)
        after = ruling_identities(after_rows)
        if step.fail:
            self._guard(
                "untouched",
                self._check_untouched,
                key,
                before_rows,
                after_rows,
                search_before,
                expect_failed and not reported_failed,
                self.events[_EXTRACTION_FAILED_EVENT] == logged_before,
            )
        self._guard("gap", self._check_gap, before, after)
        self._guard("alert", self._check_alerts, key, after_rows)
        self._guard("search", self._check_search, key, after_rows)
        self._guard("search_fields", self._check_search_fields)
        self._guard("telemetry", self._check_telemetry, key)
        if step.kind != "db" and not step.whole and not step.fail:
            # A DB-row reingest only rewrites rows it selects, so it can
            # leave a slot untouched; the worker writes every ruling row.
            self._guard("synthetic_title", self._check_rows, key, after_rows, step)
        doc = self.event_extra.get("ruling_text", step.doc) if step.kind != "db" else step.doc
        if not step.fail:
            # A failed step writes nothing (``untouched``), so its document
            # text decides nothing.
            self._guard("evidence", self._check_evidence, key, step, doc, before_rows, after_rows)
        judges_after = self._judge_rows(key)
        self._guard("judge_converge", self._check_one_judge, key, judges_after)
        self._guard(
            "judge_converge",
            self._check_renames,
            judges_before,
            judges_after,
            spelling_before,
        )

    def write_legacy_row(self, key: Key, document_id: str, spec: Spec) -> None:
        """Write one document + ruling on *key* directly, the way an older
        writer left it (no split set, no search doc).  For scenarios whose
        starting state today's writers no longer produce."""
        from ingestion.db import insert_document_and_ruling, upsert_case, upsert_court

        court_id = upsert_court(
            self.real, "CA", key.county, f"Superior Court, County of {key.county}"
        )
        case_id = upsert_case(
            self.real,
            spec.case_number or f"UNKNOWN-{document_id}",
            court_id,
            case_title=spec.title,
        )
        insert_document_and_ruling(
            self.real,
            document_id=document_id,
            case_id=case_id,
            court_id=court_id,
            content_format="pdf",
            content_hash=key.content_hash,
            s3_key=key.s3_key,
            s3_bucket="harness-bucket",
            source_url="",
            scraper_id="legacy-writer",
            captured_at=CAPTURED_AT,
            hearing_date=HEARING,
            ruling_text=spec.text,
            hearing_date_source="llm",
        )
        self.real.commit()
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT ruling_text FROM rulings WHERE document_id = %s::uuid", (document_id,)
            )
            self.indexer.docs[document_id] = cur.fetchone()[0]

    def _fail(self, invariant: str, message: str) -> None:
        raise InvariantError(invariant, f"{self._step_label}: {message}")

    def _guard(self, invariant: str, check: Callable[..., None], *args: Any) -> None:
        """Run *check*, which enforces *invariant*; ignore its failure when
        this run does not enforce that invariant."""
        try:
            check(*args)
        except InvariantError as exc:
            if exc.invariant in self.invariants:
                raise

    def _after_commit(self) -> None:
        """Check the committed state.  A violation is recorded, not raised
        here, so the code under test cannot swallow it; ``run_step`` raises
        the first one when the step ends."""
        key = self._key
        if key is None or self._pending is not None:
            return
        rows = read_key(self.checker, key.s3_key)
        self._snapshots.append(ruling_identities(rows))
        self._validation_seen |= self._validation_ids(key)
        try:
            self._guard("superseded", self._check_supersede, key, rows)
            self._guard("date_source", self._check_date_source, key, rows)
        except InvariantError as exc:
            self._pending = exc

    def _search_docs(self, key: Key) -> dict[str, str | None]:
        """The key's search docs: ``{document id: indexed ruling text}``."""
        ids = {key.parent, *key.slots}
        return {d: t for d, t in self.indexer.docs.items() if d in ids}

    def _check_untouched(
        self,
        key: Key,
        before: list[DocRow],
        after: list[DocRow],
        search_before: dict[str, str | None],
        not_reported: bool,
        not_logged: bool,
    ) -> None:
        def show(rows: list[DocRow]) -> list[tuple[str, str, str | None, str | None]]:
            return [(key.label(r.id), r.status, r.ruling_id, r.case_number) for r in rows]

        if after != before:
            self._fail(
                "untouched",
                f"a failed extraction changed the key's rows: before {show(before)}, "
                f"after {show(after)}",
            )
        search_after = self._search_docs(key)
        if search_after != search_before:
            self._fail(
                "untouched",
                f"a failed extraction changed the key's search docs: before "
                f"{sorted(key.label(d) for d in search_before)}, after "
                f"{sorted(key.label(d) for d in search_after)}",
            )
        if not_logged:
            # Without the failure branch's log, a step that skipped the
            # extraction for another reason would pass vacuously.
            self._fail(
                "untouched",
                f"the step did not take the failed-extraction branch (no "
                f"{_EXTRACTION_FAILED_EVENT} event logged)",
            )
        if not_reported:
            self._fail(
                "untouched",
                "a failed extraction on a split-set replacement did not report the "
                "key as failed (no SplitExtractionFailedError), so the reingest's "
                "failed-keys manifest would not retry it",
            )

    def _check_date_source(self, key: Key, rows: list[DocRow]) -> None:
        for r in rows:
            if r.ruling_id and r.hearing_date is not None and r.hearing_date_source is None:
                self._fail(
                    "date_source",
                    f"ruling on {key.label(r.id)} has hearing_date {r.hearing_date} "
                    "and no hearing_date_source",
                )

    def _validation_ids(self, key: Key) -> set[str]:
        """Committed ``validation_results`` row ids for *key*: rows that
        carry its S3 key, or name one of its document ids."""
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM validation_results "
                "WHERE s3_key = %s OR document_id = ANY(%s::uuid[])",
                (key.s3_key, [key.parent, *key.slots]),
            )
            return {row[0] for row in cur.fetchall()}

    # -- invariants -----------------------------------------------------

    def _check_telemetry(self, key: Key) -> None:
        gone = self._validation_seen - self._validation_ids(key)
        if gone:
            self._fail(
                "telemetry",
                f"{len(gone)} committed validation_results row(s) were deleted: {sorted(gone)}",
            )

    def _check_gap(self, before: Counter[tuple[str, str]], after: Counter[tuple[str, str]]) -> None:
        kept = set(before) & set(after)
        for n, snap in enumerate(self._snapshots):
            missing = sorted(i for i in kept if snap[i] == 0)
            if missing:
                self._fail(
                    "gap",
                    f"after commit {n + 1}/{len(self._snapshots)} ruling(s) "
                    f"{[(t[:24], c) for t, c in missing]} were missing; they exist "
                    "before and after the step",
                )

    def _check_supersede(self, key: Key, rows: list[DocRow]) -> None:
        for r in rows:
            if r.status == "superseded" and r.ruling_id:
                self._fail("superseded", f"superseded document {key.label(r.id)} holds a ruling")
        links = {r.id: r.previous_version_id for r in rows if r.previous_version_id}
        for start in links:
            seen = {start}
            cur = links.get(start)
            while cur is not None:
                if cur in seen:
                    self._fail(
                        "superseded",
                        f"previous_version_id cycle through {key.label(start)}",
                    )
                seen.add(cur)
                cur = links.get(cur)

    def _check_search(self, key: Key, rows: list[DocRow]) -> None:
        holding = {r.id: r.text for r in rows if r.ruling_id}
        on_key = {r.id for r in rows} | set(key.slots) | {key.parent}
        indexed = {d: t for d, t in self.indexer.docs.items() if d in on_key}
        for doc_id, text in holding.items():
            if doc_id not in indexed:
                self._fail("search", f"ruling on {key.label(doc_id)} has no search doc")
            if indexed[doc_id] != text:
                self._fail(
                    "search",
                    f"search doc of {key.label(doc_id)} holds "
                    f"{(indexed[doc_id] or '')[:24]!r}, the ruling is {(text or '')[:24]!r}",
                )
        for doc_id in indexed:
            if doc_id not in holding:
                self._fail(
                    "search",
                    f"search doc {key.label(doc_id)} exists without a ruling "
                    f"(holds {(indexed[doc_id] or '')[:24]!r})",
                )

    def _check_search_fields(self) -> None:
        """Every search doc the worker wrote matches the doc built from
        ``derived.*`` now, on every key, not only the one this step wrote:
        a write that renames a judge changes the judge name of rulings it
        never touched (#4920)."""
        if not self.indexer.meta:
            return
        expected = load_search_events(self.checker, list(self.indexer.meta))
        for doc_id, have in sorted(self.indexer.meta.items()):
            event = expected.get(doc_id)
            if event is None:
                continue  # no ruling: the ``search`` invariant covers it
            want = search_doc_metadata(event)
            drift = sorted(f for f in want if want[f] != have.get(f))
            if drift:
                name = drift[0]
                self._fail(
                    "search_fields",
                    f"search doc {doc_id[:8]} has {name}={have.get(name)!r}, "
                    f"derived.* has {want[name]!r} (drifted fields: {drift})",
                )

    def _check_rows(self, key: Key, rows: list[DocRow], step: Step) -> None:
        """A ruling on its own synthetic case carries its own caption.

        Only rulings this step writes are checked: a titleless ruling with no
        case number fails validation (``unknown_and_null_title_fail``) and is
        not written, so what its slot holds is not this step's.
        """
        written = {
            s.text: s.title
            for s in step.specs
            if s.case_number is None
            and s.title is not None
            and any(r.kind == "ruling" and step.specs[r.spec].text == s.text for r in step.rows)
        }
        for r in rows:
            if not r.ruling_id or r.case_number != f"UNKNOWN-{r.id}":
                continue
            want = written.get(r.text or "")
            if want is not None and r.case_title != want:
                self._fail(
                    "synthetic_title",
                    f"ruling on {key.label(r.id)} has caption {want!r} but its "
                    f"synthetic case carries {r.case_title!r}",
                )

    def _check_evidence(
        self,
        key: Key,
        step: Step,
        doc: str,
        before_rows: list[DocRow],
        after_rows: list[DocRow],
    ) -> None:
        """Every ruling is filed under a number the document prints, or under
        its ``UNKNOWN-`` case, once the document's text can settle it.

        A live step keeps an established case link (preserve-first, #2475,
        #4788): a ruling that kept its row and its case on a live step is
        exempt.  A reingest moves it off a number its document lacks (#4915).
        """
        if not doc_is_evidence(doc):
            return
        held = {(r.ruling_id, r.case_number) for r in before_rows if r.ruling_id}
        for r in after_rows:
            number = r.case_number
            if not r.ruling_id or not number or number.startswith("UNKNOWN-"):
                continue
            if step.kind == "live" and (r.ruling_id, number) in held:
                continue
            if not case_number_supported_by_text(number, doc):
                self._fail(
                    "evidence",
                    f"ruling {(r.text or '')[:24]!r} on {key.label(r.id)} is filed under "
                    f"{number!r}, which its document's text does not print",
                )

    # -- judges ---------------------------------------------------------

    def _ensure_roster(self, key: Key, roster: tuple[str, ...]) -> None:
        """Give the key's court a roster snapshot of *roster* before its
        first step (``_get_roster_names`` reads the latest one)."""
        if not roster or key.s3_key in self._rostered:
            return
        from ingestion.db import _derive_court_code, clear_roster_cache

        snapshot_court = _derive_court_code("CA", key.county).replace("-", "_")
        mapping = {f"harness-{n}": name for n, name in enumerate(roster)}
        with self.checker.cursor() as cur:
            cur.execute(
                "INSERT INTO court_directory_snapshots "
                "(court_id, captured_at, s3_key, mapping, content_hash) "
                "VALUES (%s, %s, %s, %s::jsonb, %s)",
                (
                    snapshot_court,
                    CAPTURED_AT,
                    f"harness/roster/{snapshot_court}.json",
                    json.dumps(mapping),
                    hashlib.sha256(json.dumps(mapping).encode()).hexdigest(),
                ),
            )
        clear_roster_cache()
        self._rostered.add(key.s3_key)

    def _judge_rows(self, key: Key) -> list[tuple[str, str, str, str]]:
        """``(document id, ruling text, judge id, canonical name)`` of every
        ruling on *key* that has a judge."""
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT d.id::text, r.ruling_text, j.id::text, j.canonical_name "
                "FROM rulings r JOIN documents d ON d.id = r.document_id "
                "JOIN judges j ON j.id = r.judge_id WHERE d.s3_key = %s",
                (key.s3_key,),
            )
            return [tuple(row) for row in cur.fetchall()]

    def _check_one_judge(self, key: Key, rows: list[tuple[str, str, str, str]]) -> None:
        """A court's rulings of one roster judge share one judge row,
        whatever spelling their documents print (#2140, #4926)."""
        by_judge: dict[str, dict[str, str]] = {}
        for _doc, text, judge_id, canonical in rows:
            judge = roster_judge(self._spelling.get(text))
            if judge is not None:
                by_judge.setdefault(judge.roster, {})[judge_id] = canonical
        for roster, judges in sorted(by_judge.items()):
            if len(judges) > 1:
                self._fail(
                    "judge_converge",
                    f"rulings of roster judge {roster!r} resolve to {len(judges)} judges: "
                    f"{sorted(judges.values())}",
                )

    def _check_renames(
        self,
        before: list[tuple[str, str, str, str]],
        after: list[tuple[str, str, str, str]],
        spelling_before: dict[str, str | None],
    ) -> None:
        """A judge takes a new name only when two documents support it (#4928).

        Within a family of spellings (``_normalize_for_comparison``: the
        same words, initials aside) the new spelling itself needs two
        documents; a move to another family (a typo's, or away from one)
        needs two documents printing that family.  So one typo document
        never renames a judge, and neither does one extra initial.

        A document counts for the spelling it printed before the step or
        the one it prints now: arbitration runs while the step's own rows
        and the key's earlier rows are both in place.
        """
        names_before = {judge_id: name for _d, _t, judge_id, name in before}
        if not names_before:
            return
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT id::text, canonical_name FROM judges WHERE id = ANY(%s::uuid[])",
                (sorted(names_before),),
            )
            names_now = dict(cur.fetchall())
        for judge_id, old in sorted(names_before.items()):
            new = names_now.get(judge_id)
            if new is None or new.lower() == old.lower():
                continue
            if _family(new) == _family(old):
                target, what = new.lower(), "spelling"

                def key_of(s: str | None) -> str:
                    return _spelled(s)
            else:
                target, what = _family(new), "family of spellings"

                def key_of(s: str | None) -> str:
                    return _family(_spelled(s))

            printing = {d for d, t, _j, _n in before if key_of(spelling_before.get(t)) == target}
            printing |= {d for d, t, _j, _n in after if key_of(self._spelling.get(t)) == target}
            if len(printing) < 2:
                self._fail(
                    "judge_converge",
                    f"judge {old!r} was renamed {new!r}, a {what} "
                    f"{len(printing)} document(s) print",
                )

    def _roster_votes(self, key: Key) -> dict[str, tuple[set[str], Counter[str]]]:
        """Per roster judge: the canonical names its rulings on *key* carry,
        and how many documents print each spelling."""
        out: dict[str, tuple[set[str], Counter[str]]] = {}
        for _doc, text, _judge_id, canonical in self._judge_rows(key):
            spelling = self._spelling.get(text)
            judge = roster_judge(spelling)
            if judge is None or spelling is None:
                continue
            names, votes = out.setdefault(judge.roster, (set(), Counter()))
            names.add(canonical)
            votes[spelling] += 1
        return out

    def _check_order(self, forward: Key, flipped: Key) -> None:
        """When a judge's documents decide its name, the order they arrive
        in does not change it (#4922, #4928).

        The documents decide the name when ``_choose_canonical_spelling``
        picks the same spelling whatever the current name; close counts keep
        whichever name came first, by design.
        """
        ahead = self._roster_votes(forward)
        behind = self._roster_votes(flipped)
        for roster in sorted(set(ahead) | set(behind)):
            names_a, votes = ahead.get(roster, (set(), Counter()))
            names_b, _ = behind.get(roster, (set(), Counter()))
            if {n.lower() for n in names_a} == {n.lower() for n in names_b}:
                continue
            evidence = sorted(votes.items())
            winners = {
                _choose_canonical_spelling(normalize_judge_name(s) or s, evidence)[0].lower()
                for s in votes
            }
            if len(winners) == 1:
                self._fail(
                    "judge_converge",
                    f"documents printing {dict(evidence)} decide roster judge {roster!r} is "
                    f"{winners.pop()!r}, but one row order leaves {sorted(names_a)} and the "
                    f"other {sorted(names_b)}",
                )

    # -- alerts ---------------------------------------------------------

    def _attach_alerts(self, key: Key) -> None:
        """Give every ruling on *key* that has none an alert."""
        with self.checker.cursor() as cur:
            if self._user_sub is None:
                cur.execute(
                    "INSERT INTO users (email) VALUES (%s) RETURNING id",
                    (f"{uuid.uuid4().hex}@harness.test",),
                )
                user_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO alert_subscriptions (user_id, alert_type) "
                    "VALUES (%s, (SELECT enum_range(NULL::alert_type))[1]) RETURNING id",
                    (user_id,),
                )
                self._user_sub = str(cur.fetchone()[0])
            cur.execute(
                "SELECT r.id::text, r.document_id::text, r.ruling_text, c.case_number "
                "FROM rulings r JOIN documents d ON d.id = r.document_id "
                "JOIN cases c ON c.id = r.case_id WHERE d.s3_key = %s "
                "AND NOT EXISTS (SELECT 1 FROM alert_events a WHERE a.ruling_id = r.id)",
                (key.s3_key,),
            )
            for ruling_id, doc_id, text, case_number in cur.fetchall():
                cur.execute(
                    "INSERT INTO alert_events (subscription_id, document_id, ruling_id) "
                    "VALUES (%s::uuid, %s::uuid, %s::uuid) RETURNING id",
                    (self._user_sub, doc_id, ruling_id),
                )
                self._alerts[str(cur.fetchone()[0])] = (
                    key.s3_key,
                    (text, _norm_case(case_number)),
                )

    def _check_alerts(self, key: Key, rows: list[DocRow]) -> None:
        """Every ruling that survives a step keeps its alert.

        Rulings are matched by identity, and one identity can be held by
        several rulings (the same text on two synthetic ``UNKNOWN`` cases), so
        the check counts: with *k* rulings of an identity left, at least
        ``min(k, alerts)`` of that identity's alerts must still point at one.
        An alert whose ruling is gone was rightly detached and is retired; a
        later step that brings the text back makes a new ruling, not that one.
        An alert whose ruling now has another identity (its text or case
        changed) counts for that identity from now on: when a step folds two
        rulings of one identity into one, the survivor may be the one whose
        alert was filed under its earlier text (#4929, seed 15).
        """
        present = ruling_identities(rows)
        recorded: Counter[tuple[str, str]] = Counter()
        attached: Counter[tuple[str, str]] = Counter()
        with self.checker.cursor() as cur:
            for alert_id, (s3_key, identity) in list(self._alerts.items()):
                if s3_key != key.s3_key:
                    continue
                recorded[identity] += 1
                cur.execute(
                    "SELECT r.ruling_text, c.case_number FROM alert_events a "
                    "LEFT JOIN rulings r ON r.id = a.ruling_id "
                    "LEFT JOIN cases c ON c.id = r.case_id WHERE a.id = %s::uuid",
                    (alert_id,),
                )
                text, case_number = cur.fetchone()
                if text is None:
                    del self._alerts[alert_id]
                    continue
                now = (text, _norm_case(case_number))
                attached[now] += 1
                self._alerts[alert_id] = (s3_key, now)
        for identity, count in sorted(recorded.items()):
            if attached[identity] < min(present[identity], count):
                self._fail(
                    "alert",
                    f"alert on ruling {(identity[0] or '')[:24]!r} ({identity[1]}) lost "
                    "its ruling_id while the ruling still exists",
                )


# ---------------------------------------------------------------------------
# Whole-sequence driver
# ---------------------------------------------------------------------------


def normalized(key: Key, rows: list[DocRow]) -> tuple[frozenset[tuple], frozenset[str]]:
    """Rulings as ``(slot, text, case number, caption)`` plus the active
    slots, for comparing two keys holding the same bytes.

    The caption is compared only for a synthetic ``UNKNOWN-*`` case, which
    belongs to its one document.  A real case is shared by every capture of
    that case and keeps the first caption it got (#2006), so its caption
    depends on what else was ingested, not on this key's history.
    """
    rulings = frozenset(
        (
            key.label(r.id),
            r.text,
            _norm_case(r.case_number),
            r.case_title if _norm_case(r.case_number) == "UNKNOWN" else None,
        )
        for r in rows
        if r.ruling_id
    )
    active = frozenset(key.label(r.id) for r in rows if r.status == "active")
    return rulings, active


def run_seed(
    dsn: str,
    seed: int,
    run_token: str,
    *,
    name: str = "seq",
    invariants: frozenset[str] | None = None,
    whole_documents: bool | None = None,
) -> Counter[str]:
    """Run *seed*'s sequence and the convergence checks; raise
    :class:`InvariantError` on the first broken invariant.  Two runs of one
    seed in one session need distinct *name*s (their keys derive from it).
    *invariants* limits the check to those invariants (default: all);
    *whole_documents* is :func:`generate`'s.  Returns the
    ``telemetry_event`` counts the run logged."""
    steps = generate(seed, whole_documents=whole_documents)
    return run_steps(dsn, steps, run_token, seed=seed, name=name, invariants=invariants)


def run_steps(
    dsn: str,
    steps: list[Step],
    run_token: str,
    *,
    seed: int = -1,
    name: str = "seq",
    setup: Callable[[KeyHarness, Key], None] | None = None,
    invariants: frozenset[str] | None = None,
) -> Counter[str]:
    """Run *steps* on one key, then the convergence checks.  *setup*, when
    given, prepares the key's starting state; *invariants* limits the check
    to those invariants (default: all).  Returns the ``telemetry_event``
    counts the run logged."""
    harness = KeyHarness(dsn, run_token, seed, invariants)
    try:
        key = make_key(f"{run_token}-{seed}-{name}-main")
        if setup is not None:
            setup(harness, key)
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")

        final = steps[-1]
        no_rows = not read_key(harness.checker, key.s3_key)
        if final.kind == "live" or (final.kind == "db" and no_rows):
            # A live write keeps an established case link (preserve-first,
            # #2475), so its result may depend on history by design, and a
            # DB-row reingest only re-ingests keys that have rows.  A
            # split-set replacement must not depend on history: re-ingest
            # the same bytes.
            settle = replace(final, kind="reingest", ops=("settle",))
            harness.run_step(key, settle, "settle")
        history = normalized(key, read_key(harness.checker, key.s3_key))

        fresh = {}
        fresh_keys = {}
        for kind in ("reingest", "live"):
            fresh_key = make_key(f"{run_token}-{seed}-{name}-fresh-{kind}")
            harness.run_step(fresh_key, replace(final, kind=kind), f"fresh {kind}")
            fresh[kind] = normalized(fresh_key, read_key(harness.checker, fresh_key.s3_key))
            fresh_keys[kind] = fresh_key

        if final.roster and any(r.kind == "ruling" for r in final.rows) and not final.whole:
            # The same document with its rows in the opposite order: the
            # judge's documents vote in the opposite order (#4922).
            reversed_key = make_key(f"{run_token}-{seed}-{name}-fresh-reversed")
            flipped = replace(final, kind="reingest", rows=tuple(reversed(final.rows)))
            harness.run_step(reversed_key, flipped, "fresh reversed")
            harness._guard(
                "judge_converge",
                harness._check_order,
                fresh_keys["reingest"],
                reversed_key,
            )

        if "converge" not in harness.invariants:
            return harness.events
        # Without document text to settle a number, the enrichment step's
        # fuzzy correction moves a number onto a near-miss case the court
        # already holds (#1576), which depends on the court's history.
        near_miss_ok = not doc_is_evidence(final.doc)
        if not _converged(history, fresh["reingest"], near_miss_ok=near_miss_ok):
            raise InvariantError(
                "converge",
                f"seed={seed}: reingest after the history leaves {_fmt(history)}, "
                f"a fresh reingest of the same bytes leaves {_fmt(fresh['reingest'])}",
            )
        if fresh["live"] != fresh["reingest"]:
            raise InvariantError(
                "converge",
                f"seed={seed}: a fresh live capture leaves {_fmt(fresh['live'])}, a "
                f"fresh reingest of the same bytes leaves {_fmt(fresh['reingest'])}",
            )
        return harness.events
    finally:
        harness.close()


def run_shared_case_retitle(
    dsn: str, run_token: str, first_kind: str, second_kind: str, *, name: str = "retitle"
) -> None:
    """Two keys in one county each write a ruling of one shared case; the
    first has no caption, the second gives the case its title (#4935).

    The second key's write changes ``cases.case_title``, which every search
    doc of the case shows, so the first key's search doc must follow: the
    ``search_fields`` invariant checks every key the harness wrote.  The
    second key's extraction holds two rulings (a split batch); the first
    key's holds one.  Raises :class:`InvariantError` on a broken invariant.
    """
    tag = f"{run_token}-{name}-{first_kind}-{second_kind}"
    shared = 7
    harness = KeyHarness(dsn, run_token, -1, frozenset({"search", "search_fields"}))
    try:
        first = make_key(f"{tag}-a", county_tag=tag)
        second = make_key(f"{tag}-b", county_tag=tag)
        untitled = Spec(shared, _case_number(shared), None, _text(f"{tag}-a", shared))
        harness.run_step(first, Step(first_kind, (Row("ruling", 0),), (untitled,)), "untitled")
        titled = (
            Spec(shared, _case_number(shared), _title(shared), _text(f"{tag}-b", shared)),
            Spec(1, _case_number(1), _title(1), _text(f"{tag}-b", 1)),
        )
        harness.run_step(
            second,
            Step(second_kind, (Row("ruling", 0), Row("ruling", 1)), titled, ("retitle_shared",)),
            "retitle",
        )
        titles = {
            meta.get("case_title")
            for doc_id, meta in harness.indexer.meta.items()
            if doc_id == first.parent or doc_id in first.slots
        }
        if titles != {_title(shared)}:
            raise InvariantError(
                "search_fields",
                f"{tag}: the first key's search doc shows case titles {titles}, "
                f"the shared case is titled {_title(shared)!r}",
            )
    finally:
        harness.close()


def _converged(
    history: tuple[frozenset[tuple], frozenset[str]],
    fresh: tuple[frozenset[tuple], frozenset[str]],
    *,
    near_miss_ok: bool = False,
) -> bool:
    """Whether a key re-ingested after a history holds the rows a fresh
    ingest of the same bytes holds.

    One difference is allowed: where the fresh ingest found no case number
    (``UNKNOWN``), the key may keep the real case its ruling had before.  A
    re-extraction that misses a case number must not throw away the one an
    earlier extraction of the same bytes found (#4788).  With
    *near_miss_ok*, the two may also hold case numbers within the fuzzy
    matcher's reach of each other (the enrichment correction, which reads
    the court's existing cases).
    """
    if history[1] != fresh[1]:
        return False
    hist = {slot: rest for slot, *rest in history[0]}
    new = {slot: rest for slot, *rest in fresh[0]}
    if hist.keys() != new.keys() or len(hist) != len(history[0]) or len(new) != len(fresh[0]):
        return history == fresh
    for slot, (text, case, title) in new.items():
        h_text, h_case, h_title = hist[slot]
        if h_text != text:
            return False
        if h_case == case and h_title == title:
            continue
        if case == "UNKNOWN" and h_case != "UNKNOWN":
            continue
        if near_miss_ok and _near_miss(case, h_case):
            continue
        return False
    return True


def _near_miss(a: str, b: str) -> bool:
    """Whether two real case numbers are within the enrichment fuzzy
    matcher's distance (2) of each other."""
    from framework.enrichment import levenshtein_distance, normalize_case_number

    if "UNKNOWN" in (a, b):
        return False
    return levenshtein_distance(normalize_case_number(a), normalize_case_number(b)) <= 2


def _fmt(state: tuple[frozenset[tuple], frozenset[str]]) -> str:
    rulings, active = state
    items = sorted(
        (slot, (text or "").split(".")[0].removeprefix("Ruling "), case, title)
        for slot, text, case, title in rulings
    )
    return f"rulings={items} active={sorted(active)}"


@contextmanager
def db_mode_driver(driver: DbModeDriver) -> Iterator[None]:
    """Temporarily replace :data:`DB_MODE_DRIVER`."""
    global DB_MODE_DRIVER
    saved = DB_MODE_DRIVER
    DB_MODE_DRIVER = driver
    try:
        yield
    finally:
        DB_MODE_DRIVER = saved


# ---------------------------------------------------------------------------
# Deterministic splitters: a child's enrichment is exhausted (#4878)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicSplitter:
    """One of the worker's deterministic splitters (``_try_*_split``).

    The harness stubs the court module's ``_split_rulings`` (and, for the
    HTML splitters, the content detector); the splitter's dispatch loop and
    everything after it are the production code.  Its county and format
    gates are real, so the event carries the real county.
    """

    name: str
    county: str
    content_format: str
    module: str
    detector: str | None = None


DETERMINISTIC_SPLITTERS = (
    DeterministicSplitter(
        "sd", "San Diego", "html", "courts.ca.sd_calendar", "is_sd_calendar_html"
    ),
    DeterministicSplitter("la", "Los Angeles", "html", "courts.ca.la_tentatives", "is_la_html"),
    DeterministicSplitter("fresno", "Fresno", "pdf", "courts.ca.fresno_tentatives"),
    DeterministicSplitter("riverside", "Riverside", "pdf", "courts.ca.riverside_tentatives"),
    DeterministicSplitter("sf", "San Francisco", "pdf", "courts.ca.sf_tentatives"),
    DeterministicSplitter("sc", "Santa Clara", "pdf", "courts.ca.sc_tentatives"),
)


def _split_ruling(spec: Spec) -> Any:
    """A court splitter's result for *spec* (the fields every splitter
    reads).  ``outcome`` is left unset, so per-child enrichment runs."""
    from types import SimpleNamespace

    return SimpleNamespace(
        case_number=spec.case_number,
        case_title=spec.title,
        ruling_text=spec.text,
        ruling_text_html=None,
        hearing_date=HEARING,
        department="1",
        motion_type="demurrer",
        outcome=None,
        parties=[],
        judge_name=None,
    )


@contextmanager
def deterministic_split(
    harness: KeyHarness,
    splitter: DeterministicSplitter,
    specs: list[Spec],
    failing: Callable[[], set[str]],
) -> Iterator[None]:
    """Route the harness worker's events through *splitter*, which splits
    each document into *specs*.  Per-child enrichment is exhausted for a
    child whose text is in ``failing()`` at the time it runs."""
    import importlib

    module = importlib.import_module(splitter.module)

    def enrich(ruling_text: str, _document_id: str) -> tuple[Any, str]:
        if ruling_text in failing():
            return None, "errored"
        return None, "skipped_disabled"

    patches = [
        patch.object(module, "_split_rulings", lambda *_a, **_k: [_split_ruling(s) for s in specs]),
        patch.object(harness.worker, "_llm_enrich_fields", enrich),
    ]
    if splitter.detector:
        patches.append(patch.object(module, splitter.detector, lambda *_a, **_k: True))
    harness.event_extra = {
        "county": splitter.county,
        "content_format": splitter.content_format,
        "ruling_text": f"{splitter.name} harness document",
    }
    try:
        for p in patches:
            p.start()
        yield
    finally:
        for p in reversed(patches):
            p.stop()
        harness.event_extra = {}


def run_enrichment_failure(
    dsn: str, splitter: DeterministicSplitter, seed: int, run_token: str
) -> None:
    """Ingest a key through *splitter* with enrichment healthy, then re-ingest
    it while enrichment is exhausted for one random child (or, sometimes,
    every child).  Each failed child must keep the document and ruling its
    slot held (``kept``), and the step must hold the other invariants."""
    from ingestion.split_ids import split_child_document_id

    rng = random.Random(seed)
    token = f"{run_token}-{splitter.name}-{seed}"
    n_specs = rng.randint(2, 6)
    specs = [
        Spec(
            idx=i,
            case_number=_county_case_number(splitter.county, i),
            title=None if rng.random() < 0.3 else _title(i),
            text=_text(token, i),
        )
        for i in range(n_specs)
    ]
    rows = tuple(Row("ruling", i) for i in range(n_specs))
    failed = list(range(n_specs)) if rng.random() < 0.25 else [rng.randrange(n_specs)]
    kinds = [rng.choice(("live", "reingest")) for _ in range(2)]

    harness = KeyHarness(dsn, run_token, seed)
    failing: set[str] = set()
    try:
        key = replace(make_key(f"{token}-key"), county=splitter.county)
        with deterministic_split(harness, splitter, specs, lambda: failing):
            first = Step(kinds[0], rows, tuple(specs), (splitter.name,))
            harness.run_step(key, first, "healthy")
            before = {r.id: r for r in read_key(harness.checker, key.s3_key)}
            child_ids = [split_child_document_id(key.parent, i, n_specs) for i in failed]
            for i, doc_id in zip(failed, child_ids, strict=True):
                held = before.get(doc_id)
                if held is None or held.text != specs[i].text:
                    raise InvariantError(
                        "error",
                        f"seed={seed} {splitter.name}: the healthy ingest did not write "
                        f"ruling {i} to its slot (the scenario would prove nothing)",
                    )

            failing.update(specs[i].text for i in failed)
            second = Step(kinds[1], rows, tuple(specs), (splitter.name, f"fail={failed}"))
            harness.run_step(key, second, "enrichment exhausted")
            after = {r.id: r for r in read_key(harness.checker, key.s3_key)}
            for i, doc_id in zip(failed, child_ids, strict=True):
                held, now = before[doc_id], after.get(doc_id)
                if now is None or (now.ruling_id, now.text) != (held.ruling_id, held.text):
                    raise InvariantError(
                        "kept",
                        f"seed={seed} {splitter.name} {second.describe()}: child {i} "
                        f"({key.label(doc_id)}) failed enrichment and lost its ruling "
                        f"(had {held.ruling_id}, now "
                        f"{None if now is None else now.ruling_id})",
                    )
    finally:
        harness.close()


# ---------------------------------------------------------------------------
# A failed extraction leaves the key alone (#4948)
# ---------------------------------------------------------------------------


def run_extraction_failure(
    dsn: str,
    seed: int,
    run_token: str,
    *,
    name: str = "fail",
    steps: list[Step] | None = None,
    kind: str | None = None,
) -> None:
    """Run a history on a key (*steps*, default *seed*'s generated
    sequence), then re-ingest it while every extraction call fails
    (``ExtractionIncompleteError``).  The failed step must leave the key's
    rows and search docs exactly as they were (``untouched``) and hold the
    other invariants; a split-set replacement must report the key as
    failed.  Raises :class:`InvariantError` on a broken invariant.

    The failed step is *kind*, or one the seed picks: a prefix reingest, a
    DB-row reingest or a live capture (the last two only when the key
    holds rows).
    """
    steps = steps if steps is not None else generate(seed)
    rng = random.Random(f"{seed}:fail")
    harness = KeyHarness(dsn, run_token, seed)
    try:
        key = make_key(f"{run_token}-{seed}-{name}")
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")
        if kind is None:
            has_rows = bool(read_key(harness.checker, key.s3_key))
            kind = rng.choice(("reingest", "db", "live") if has_rows else ("reingest",))
        failed = replace(steps[-1], kind=kind, fail=True, whole=False)
        harness.run_step(key, failed, "extraction failed")
    finally:
        harness.close()
