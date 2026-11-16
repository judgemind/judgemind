"""Invariant harness for split / dedup / supersede / relink (#4845).

Drives random sequences of split results for one S3 key through the real
write paths against a real Postgres, and checks after **every commit** the
properties that must hold for any sequence of re-splits:

- ``gap``: a ruling that exists before and after a step is never missing at
  a commit in between (no ruling-less interval);
- ``alert``: a ``public.alert_events`` row keeps its ``ruling_id`` while its
  ruling still exists;
- ``alert_case``: an alert follows only its own ruling: a row whose alert
  carried over holds the same generated ruling (a re-extracted or relinked
  case number) or the same real case (re-extracted text).  Another ruling
  on a synthetic ``UNKNOWN`` case, or on a different case, is a different
  ruling, so the old row is retired and its alert detached, never moved to
  it (#4946);
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
  #4915, #4927), on a live step as on a reingest (#5006).  A scraper's
  pre-split child keeps an established case link (preserve-first, #2475),
  so on a ``presplit`` step a ruling that kept its row and its case is
  exempt.  An LLM's misread or invented number, whichever
  extractor (the LLM split, the regex fallback over the LLM's
  transcription, the fuzzy correction) produced it, breaks it.
- ``judge_converge``: a court's rulings of one roster judge, whatever
  spelling their documents print, resolve to one judge row; a judge is
  renamed only to a spelling at least two documents print, so one typo
  document never renames (#4928); and when the documents decide a judge's
  name (``_choose_canonical_spelling`` returns it whatever the current
  name), ingesting the same document in the opposite row order ends on
  the same name (#4922).  Votes count only from active documents that
  still resolve to that judge: every active document on the harness's
  keys whose ruling has a judge holds exactly one ``judge_name_evidence``
  row, for that judge, and no other document holds one; and every judge
  those rulings have been on keeps the name a recount of its votes gives
  it, including a judge whose votes all moved away (#4966).
- ``untouched``: a step whose extraction fails (every page's and chunk's
  API call failed, ``ExtractionIncompleteError``) leaves the key's rows and
  search docs exactly as they were: no whole-document ruling next to the
  key's split children, no deletions (#4948).  The same holds when the
  worker, in ``required`` extraction mode, finds an extractor it needs
  could not be built (#4953), when no extractor could run at all (no
  text to read and no page images, #4968), and when a page or chunk failed
  while others answered (``PartialExtractionError``, #5000), and when the
  input itself is incomplete: a text PDF's scanned (``blank_page``) page
  whose OCR failed (:data:`INCOMPLETE_INPUT`, #5010).  Checked on every generated
  step whose extraction is incomplete, by :func:`run_extraction_failure`
  and by :func:`run_incomplete_input`.
- ``failed_manifest``: a step whose extraction did not produce a complete
  result reports the key as failed.  A split-set replacement (``reingest`` /
  ``db``) raises ``SplitExtractionFailedError``, which the reingest's
  per-key handler counts, so ``--write-failed-manifest`` retries the key;
  a text-first county's document with no text raises its subclass
  ``NoExtractableTextError``, which the reingest reports apart from the
  retry list, since a retry reads the same bytes (#4983);
  a missing extractor raises ``ExtractorUnavailableError`` on every step
  kind, a live capture included, so the live worker keeps the message for
  after its restart (#4948, #4953, #4968).
- ``refuse_start``: a worker whose required extractor's secret is missing
  refuses to start (``_require_extractors``), and so does the scripts'
  preflight (``check_extraction_ready``), whatever county configs name that
  provider (#4953, #4957).
- ``printed_somewhere``: in a court several keys share
  (:func:`run_shared_court`, #4969), every ruling a key's last extraction
  returned is held, active, by a key that lists it.  A repost on a second
  key loses content-hash dedup to the holder, so when the holder stops
  listing the ruling the reposting key must hold it (open: #4982).
- ``held_printed``: in such a court, no key holds a ruling its last
  extraction did not return (a pre-split capture keeps its earlier
  captures' rulings on other slots).
- ``explained``: a complete step that leaves its key with no document rows
  records why (#5015).  An extraction's skip verdict (``no_ruling_text``,
  ``zero_rulings_no_text``) carries the current
  ``validation.skip_versions`` rule version, so the reconciler can re-judge
  it, and never replaced rulings the key held (#5094).
- ``marker``: a step that ends incomplete leaves a durable
  ``telemetry.ingest_incomplete`` record for its key, and a later complete
  step clears it (#5008).  A live capture of a new key that takes the
  fallback (the whole document, or a partial split's answered parts)
  records ``single_document_fallback`` / ``partial_split``; a split-set
  replacement that reports the key failed records ``reingest_failed``; a
  split with an exhausted child records ``exhausted_child``
  (:func:`run_enrichment_failure`), and so does a whole-document step whose
  one child's enrichment is exhausted, on every step kind
  (:func:`run_whole_document_exhaustion`, #5035).  A complete step (a verdict of no
  rulings, or nothing readable, included) leaves no record; a step that
  writes nothing for another reason (a live capture of a key with rows, an
  unavailable extractor) leaves the record as it was.  Dead-lettering is
  outside the harness (``test_ingest_incomplete.py``).
- ``refused``: a step whose split rejects every child (validation, the
  all-NULL guard) on a key that holds rulings, with no verdict that the
  document holds none, keeps the key's rows (#5057): the worker refuses
  the empty split set, so the step is checked like one that wrote nothing
  (``untouched``; ``marker`` expects ``all_children_rejected``), and a
  ``reingest`` / ``db`` step raises ``AllChildrenRejectedError``.  A
  refusal is legitimate only after a complete extraction, on a key that
  held a ruling, and when a fresh ingest of the same bytes (the oracle,
  on a key with no history) writes no ruling either.  ``converge`` then
  allows the key to keep its history's rows.  The same holds for an
  extraction's skip verdict (``no_ruling_text``: every row textless;
  ``zero_rulings_no_text``: no rulings and no text) on a key that holds
  rulings (#5094): refused, ``marker`` expects ``empty_extraction``, a
  ``reingest`` / ``db`` step raises ``EmptyExtractionRefusedError``, and the
  fresh-key oracle records the verdict as a skip.

A shared-court scenario also runs ``search_fields`` (a court rename made by
one key's write re-indexes the other keys' rulings) and ``judge_converge``
(votes and one judge row per roster judge) across all its keys.  There is
no orphan-case invariant (every ``UNKNOWN-*`` case and ``case_judges`` row
backed by a ruling): a reingest leaves orphaned cases by design until the
opt-in sweep #4744 proposes lands.

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
  :data:`DB_MODE_DRIVER`;
- ``presplit``: a scraper that splits the page at capture time (Contra
  Costa, Los Angeles) emits one event per ruling it extracted, each with
  ``extra["pre_split"]`` and its fields filled (#4967).  Each event is
  written on its own, one commit per ruling, as a split set of one.  The
  events carry the LLM's transcription, not the page text: the worker reads
  the archived page (the step's ``doc``) from S3 as the case-number
  evidence (#4976), so ``evidence`` is checked as on a live step, against
  the LLM's numbers, misreads and inventions included.  A child cannot
  see its siblings, so a ruling whose
  slot a sibling takes before its own event arrives is retired and written
  again (its alert detached, a gap between the two commits): ``gap`` and
  ``alert`` are not checked on these steps.  ``alert_case`` is: a retired
  row's alert is detached, never moved to another ruling.
  :func:`with_presplit_s3_faults` makes the page read fail (#5012): once,
  transiently (``flaky``: the worker's in-call retry reads it, and every
  invariant, ``evidence`` included, holds as without the fault), or on
  every attempt (``down``: each child raises
  ``PreSplitEvidenceUnavailableError`` and writes nothing, checked as
  ``untouched``, so no LLM number goes in unchecked).

Extraction modes (#4968), from a fourth random stream (:func:`_with_extraction_modes`):

- the worker is built by the production path in ``required`` mode
  (``_require_extractors``, after the ``check_extraction_ready`` preflight)
  in one of three env shapes: production's (``LLM_PROVIDER=google``, no
  Anthropic key), Google with an Anthropic key too, or
  ``LLM_PROVIDER=anthropic``.  ``LlmExtractor`` is built for real (a missing
  key fails as in production), only its calls are routed to the step's rows;
- a seed's keys get one county extraction config (:data:`PROFILES`), copied
  from a real county so ``decide_extraction_strategy`` takes each branch:
  multimodal (Orange), text-first on the county's own provider (Ventura),
  text-first on the framework extractor, text-first on a provider the
  deployment covers only through that county (the #4957 startup check), or
  ``ExtractionMethod.NONE`` (Federal: every step is a whole document);
- a step has a ``fail_mode`` (:data:`FAIL_MODES`): every call fails, the
  multimodal call fails and the text fallback succeeds, or the multimodal
  call fails and the framework or county extractor cannot be built;
- a step with rulings can instead be partial (:data:`PARTIAL`, #5000, from
  a fifth stream): the split's extractor answers for every part but one
  (``PartialExtractionError``).  That is no complete split, so it is
  checked like a failed one (``untouched``, ``failed_manifest``), except
  that a live capture of a new key writes the parts that answered;
- a step can instead get a malformed LLM response (:data:`WRONG_SHAPE`,
  #5011, from a sixth stream): the real ``LlmExtractor`` parses a
  ``{"rulings": null}`` answer, which must fail the extraction, not pass
  for a verdict of no rulings;
- a whole-document step can have no text at all, so a multimodal county
  reaches ``zero_rulings_no_text`` and a text-first one has nothing to read.

:meth:`KeyHarness.expected_outcome` says what each step's extraction must
come to (complete, failed, or an extractor unavailable), independently of
the worker.

Failures raise :class:`InvariantError` naming the invariant, the seed
and the step, so a failing seed is its own reproduction:
``SPLIT_HARNESS_SEED=<n> pytest tests/test_split_invariants_pg.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import re
import sys
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
    "alert_case",
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
    "failed_manifest",
    "refuse_start",
    "printed_somewhere",
    "held_printed",
    "explained",
    "marker",
    "refused",
    "error",
)

#: ``ingestion.ingest_incomplete`` reasons (#5008).
_MARKER_FALLBACK_REASONS = ("single_document_fallback", "partial_split")
_MARKER_REINGEST_FAILED = "reingest_failed"
_MARKER_EXHAUSTED_CHILD = "exhausted_child"


#: ``ingestion.worker.SPLIT_EXTRACTION_FAILED_EVENT`` (#4948).
_EXTRACTION_FAILED_EVENT = "llm_split.extraction_failed"

#: ``ingestion.worker.EMPTY_SPLIT_SET_REFUSED_EVENT`` (#5057).
_EMPTY_REFUSED_EVENT = "split_set.empty_replacement_refused"
_MARKER_ALL_CHILDREN_REJECTED = "all_children_rejected"
#: ``ingestion.ingest_incomplete.INCOMPLETE_EMPTY_EXTRACTION`` (#5094): an
#: extraction's skip verdict refused over the key's rulings.
_MARKER_EMPTY_EXTRACTION = "empty_extraction"
_EMPTY_EXTRACTION_REFUSED_EVENT = f"{_EMPTY_REFUSED_EVENT}:{_MARKER_EMPTY_EXTRACTION}"
#: ``validation.skip_versions.EXTRACTION_SKIP_REASONS`` (#5094).
_EXTRACTION_SKIPS = ("no_ruling_text", "zero_rulings_no_text")


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
    kind: str  # live | reingest | db | presplit
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
    #: The key's county extraction config (:data:`PROFILES`, #4968).  One
    #: per seed: every key of a run, the fresh ones included, has it.
    profile: str = "default"
    #: The worker's env shape (:data:`ENVS`).  One per seed.
    env: str = "google"
    #: Which extractor fails or cannot be built (:data:`FAIL_MODES`), or
    #: ``""`` for none.
    fail_mode: str = ""
    #: A ``presplit`` step's S3 read fault on the archived page (#5012):
    #: :data:`S3_FLAKY` or :data:`S3_DOWN`, ``""`` for none.  Never
    #: generated: :func:`with_presplit_s3_faults` sets it.
    s3_read: str = ""

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
        if self.fail_mode:
            fail += f" fail_mode={self.fail_mode}"
        if (self.profile, self.env) != ("default", "google"):
            fail += f" profile={self.profile} env={self.env}"
        if self.s3_read:
            fail += f" s3={self.s3_read}"
        return f"{self.kind}[{' '.join(parts)}] doc={doc}{whole}{fail} ops={list(self.ops)}"


def doc_is_evidence(doc: str | None) -> bool:
    """Whether a document's text is long enough to settle a case number."""
    return len((doc or "").strip()) >= MIN_DOCUMENT_TEXT_CHARS


def _case_number(idx: int) -> str:
    # Five repeated digits: every pair is Levenshtein 5 apart, beyond the
    # enrichment fuzzy matcher's reach (distance 2).
    return f"30-2026-{str(idx % 10) * 5}"


#: Case-number shapes for counties whose format validation checks
#: (``case_number_matches_county_format``, #4907).  Each takes the index's
#: repeated digit and a per-tag ``(letters, digits)`` block: three repeated
#: digits keep every pair of one tag's numbers beyond the fuzzy matcher's
#: reach (distance 2).
_COUNTY_CASE_NUMBERS: dict[str, Callable[[str, str, str], str]] = {
    "Santa Clara": lambda d, letters, digits: f"{digits[:2]}{letters[:2]}{digits[2:5]}{d * 3}",
    "San Francisco": lambda d, letters, digits: f"{letters}-{digits[:2]}-{digits[2:5]}{d * 3}",
}


def _county_case_number(county: str, idx: int, tag: str) -> str:
    """Ruling *idx*'s case number in the real *county* for the key tagged *tag*.

    A deterministic splitter's county gate is real, so its keys share the
    county's real court with every other test of that splitter, on other
    xdist workers too.  A case number shared across tags would share the
    case row: one test's write retitling the case re-indexes the other
    test's rulings into the writer's stub indexer, and the other test sees
    its own search doc drift (``search_fields``, #4956).  So each tag gets
    its own numbers.  A splitter reads the number from the source
    (``CASE_NUMBER_FROM_SOURCE_KEY``), so no fuzzy correction can move a
    ruling onto another tag's near-miss number.
    """
    digest = hashlib.sha256(tag.encode()).digest()
    letters = "".join(chr(ord("A") + b % 26) for b in digest[:3])
    digits = "".join(str(b % 10) for b in digest[3:13])
    d = str(idx % 10)
    shape = _COUNTY_CASE_NUMBERS.get(county)
    if shape is not None:
        return shape(d, letters, digits)
    return f"{digits}-{d * 5}"


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


def generate(
    seed: int,
    *,
    whole_documents: bool | None = None,
    extraction_modes: bool | None = None,
) -> list[Step]:
    """Return the step sequence for *seed* (deterministic).

    The rows, ops and step kinds come from one random stream, as they did
    before #4929, so a seed's split sequence is unchanged.  The document
    text, the case number the LLM reads, the judge names and the roster
    come from a second stream (:class:`_Model`).  *whole_documents*
    (default :data:`WHOLE_DOCUMENT_STEPS`) turns the ``whole`` op on; the
    random streams are the same either way.  *extraction_modes* (default
    :data:`EXTRACTION_MODE_STEPS`) turns the extraction modes on
    (:func:`_with_extraction_modes`); off, a seed's steps are exactly the
    ones they were before #4968.
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
    if PRESPLIT_STEPS:
        steps = _with_presplit_steps(seed, steps)
    if EXTRACTION_MODE_STEPS if extraction_modes is None else extraction_modes:
        steps = _with_extraction_modes(seed, steps)
    return steps


#: Whether generated seeds include the extraction modes (#4968).
EXTRACTION_MODE_STEPS = True

#: A seed's county extraction config (:func:`profile_config`), and how often
#: each is drawn.
PROFILES = ("default", "multimodal", "llm_county", "llm_framework", "llm_other", "none")
_PROFILE_WEIGHTS = (8, 3, 3, 2, 2, 2)

#: The worker's env shapes: production's (``LLM_PROVIDER=google``, only the
#: Google key wired), Google with an Anthropic key too, and Anthropic.
ENVS = ("google", "google+anthropic", "anthropic")
_ENV_VARS: dict[str, dict[str, str]] = {
    "google": {"LLM_PROVIDER": "google", "GOOGLE_API_KEY": "harness-google-key"},
    "google+anthropic": {
        "LLM_PROVIDER": "google",
        "GOOGLE_API_KEY": "harness-google-key",
        "ANTHROPIC_API_KEY": "harness-anthropic-key",
    },
    "anthropic": {
        "LLM_PROVIDER": "anthropic",
        "GOOGLE_API_KEY": "harness-google-key",
        "ANTHROPIC_API_KEY": "harness-anthropic-key",
    },
}

#: How a step's extraction fails.  ``all``: every call fails.
#: ``multimodal_only``: the multimodal call fails, the text fallback
#: succeeds.  ``framework_missing`` / ``county_missing``: the multimodal
#: call fails and that text extractor cannot be built (a lazily built
#: extractor's init error), so a county that needs it has none (#4957).
FAIL_MODES = ("all", "multimodal_only", "framework_missing", "county_missing")

#: A partial extraction (#5000): the split's extractor (multimodal, or the
#: text-first county's) answers for every part but one, and raises
#: ``PartialExtractionError`` carrying the other parts' rulings.  Not a
#: complete split: a split-set replacement reports the key and leaves its
#: rows alone, a live capture of a key with rows writes nothing, and only a
#: live capture of a new key writes the carried rulings.  Drawn from its own
#: random stream (:func:`_with_partial_failures`), so the seeds pinned
#: against :data:`FAIL_MODES` keep their draws.
PARTIAL = "partial"

#: Whether generated seeds include :data:`PARTIAL` steps (#5000).
PARTIAL_STEPS = True

#: A malformed LLM response (#5011): the text extraction runs the real
#: ``LlmExtractor`` against a model that answers ``{"rulings": null}`` (valid
#: JSON, no rulings array), and the multimodal call fails.  The parser must
#: count that a failed chunk, so the result is the ``all`` mode's: no
#: complete extraction, never an empty one (before #5011 it passed for a
#: verdict of no rulings, replacing a key's split with a whole document).
#: Drawn from its own random stream (:func:`_with_wrong_shape_responses`).
WRONG_SHAPE = "wrong_shape"

#: The malformed response :data:`WRONG_SHAPE` steps get.
WRONG_SHAPE_RESPONSE = '{"rulings": null}'

#: Whether generated seeds include :data:`WRONG_SHAPE` steps (#5011).
WRONG_SHAPE_STEPS = True

#: Incomplete input (#5010): the step's PDF is a text page followed by a
#: scanned (``blank_page``: image only, no text layer) page whose OCR call
#: fails.  The extractors answer, but the text they would read lacks the
#: scanned page, so a text-first county's split is no complete result
#: (``untouched`` / ``failed_manifest``).  A reingest sends the PDF; a live
#: capture sends the scraper's text layer, names the page in
#: ``extra[SCANNED_PAGES_KEY]``, and the worker reads the archived PDF
#: again.  Run by :func:`run_incomplete_input`, not drawn by the generator.
INCOMPLETE_INPUT = "incomplete_input"

#: The text page of an :data:`INCOMPLETE_INPUT` PDF.
_BLANK_PAGE_TEXT_LAYER = (
    "SUPERIOR COURT OF CALIFORNIA. Tentative rulings, calendar page 1. "
    "The rulings on the following pages are scanned."
)


def blank_page_pdf() -> bytes:
    """A text page, then a scanned page: a picture with no text layer."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(40, 40, 572, 752), _BLANK_PAGE_TEXT_LAYER, fontsize=11)
    blank_page = doc.new_page(width=612, height=792)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    blank_page.insert_image(pymupdf.Rect(20, 20, 592, 772), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def profile_config(profile: str) -> Any:
    """The ``CountyExtractionConfig`` a key of *profile* has, or None
    (an unconfigured county: the multimodal default).

    Each is a real county's config, so production's configs are what the
    harness runs: Orange (multimodal), Ventura (text first, on the county's
    own Google extractor), Ventura on the framework extractor (no provider),
    Ventura on Anthropic (a provider only that county names, so the worker
    builds it at startup, #4957) and Federal (``ExtractionMethod.NONE``).
    The keys keep their own county names, so their courts and cases are
    their own (#4956).
    """
    from framework.extraction_config import _COUNTY_CONFIGS

    ventura = _COUNTY_CONFIGS[("CA", "VENTURA")]
    configs = {
        "default": None,
        "multimodal": _COUNTY_CONFIGS[("CA", "ORANGE")],
        "llm_county": ventura,
        "llm_framework": replace(ventura, provider=None, model=None),
        "llm_other": replace(ventura, provider="anthropic", model=None),
        "none": _COUNTY_CONFIGS[("FEDERAL", "FEDERAL")],
    }
    return configs[profile]


def _with_extraction_modes(seed: int, steps: list[Step]) -> list[Step]:
    """Give *steps* a county extraction config, an env shape and fail modes.

    A fourth random stream, so a seed's rows, ops, model draws and pre-split
    steps are the same with or without extraction modes.  A pre-split step
    runs no extraction (its scraper filled the fields), so it gets no fail
    mode; under ``none`` every other step is a whole document.
    """
    rng = random.Random(f"{seed}:extraction")
    profile = rng.choices(PROFILES, weights=_PROFILE_WEIGHTS)[0]
    # The Anthropic county config needs the Anthropic key, on a Google
    # framework (on an Anthropic one it is covered).
    env = "google+anthropic" if profile == "llm_other" else rng.choice(ENVS)
    out = []
    for step in steps:
        fail_draw, mode_draw, blank_draw = rng.random(), rng.choice(FAIL_MODES), rng.random()
        if step.kind == "presplit":
            out.append(replace(step, profile=profile, env=env))
            continue
        whole = step.whole or profile == "none"
        fail_mode = mode_draw if fail_draw < 0.2 and profile != "none" else ""
        doc = "" if whole and blank_draw < 0.3 else step.doc
        out.append(
            replace(step, profile=profile, env=env, whole=whole, fail_mode=fail_mode, doc=doc)
        )
    if PARTIAL_STEPS:
        out = _with_partial_failures(seed, out)
    if WRONG_SHAPE_STEPS:
        out = _with_wrong_shape_responses(seed, out)
    return out


def _with_wrong_shape_responses(seed: int, steps: list[Step]) -> list[Step]:
    """Give some of *steps*' healthy extractions a malformed LLM response
    (:data:`WRONG_SHAPE`).

    A sixth random stream, one draw per step whatever the step, so a seed's
    other streams are unchanged.  A whole document is eligible: a malformed
    answer on its one chunk is the case #5011 found.  Not a pre-split step
    (no extraction) nor an ``ExtractionMethod.NONE`` county's step.
    """
    rng = random.Random(f"{seed}:wrong_shape")
    out = []
    for step in steps:
        draw = rng.random()
        eligible = step.kind != "presplit" and not step.fail_mode and step.profile != "none"
        out.append(replace(step, fail_mode=WRONG_SHAPE) if eligible and draw < 0.08 else step)
    return out


def _with_partial_failures(seed: int, steps: list[Step]) -> list[Step]:
    """Make some of *steps*' healthy extractions partial (:data:`PARTIAL`).

    A fifth random stream, one draw per step whatever the step, so a seed's
    other streams (and its :data:`FAIL_MODES` draws) are unchanged.  Only a
    step with rulings to lose gets it: not a pre-split step (no
    extraction), not a whole document, not an ``ExtractionMethod.NONE``
    county's step.
    """
    rng = random.Random(f"{seed}:partial")
    out = []
    for step in steps:
        draw = rng.random()
        eligible = (
            step.kind != "presplit"
            and not step.fail_mode
            and not step.whole
            and step.profile != "none"
        )
        out.append(replace(step, fail_mode=PARTIAL) if eligible and draw < 0.12 else step)
    return out


#: Whether generated seeds include ``presplit`` steps (#4967, #4970): some
#: live captures become a scraper's pre-split capture of the same rows.
PRESPLIT_STEPS = True


def _with_presplit_steps(seed: int, steps: list[Step]) -> list[Step]:
    """Turn some of *steps*' live captures into pre-split captures.

    A third random stream, so a seed's rows, ops and model draws are the
    same with or without pre-split steps.  Only a capture whose extraction
    returns a ruling: a scraper that gets no rulings from its LLM falls
    back to emitting the page whole, which is a ``live`` step.
    """
    rng = random.Random(f"{seed}:presplit")
    out = []
    for step in steps:
        presplit = rng.random() < 0.5
        if (
            presplit
            and step.kind == "live"
            and not step.whole
            and any(r.kind == "ruling" for r in step.rows)
        ):
            step = replace(step, kind="presplit")
        out.append(step)
    return out


#: ``Step.s3_read``: the first read of the archived page fails with a
#: transient error (``SlowDown``), the in-call retry reads it (#5012).
S3_FLAKY = "flaky"
#: ``Step.s3_read``: every read fails with a transient error
#: (``ServiceUnavailable``), so the step's children fail closed (#5012).
S3_DOWN = "down"
#: The expected outcome of a ``presplit`` step whose page cannot be read.
EVIDENCE_UNAVAILABLE = "evidence_unavailable"


def with_presplit_s3_faults(steps: list[Step], fault: str) -> list[Step]:
    """*steps* with every ``presplit`` step's archived-page read given
    *fault* (:data:`S3_FLAKY` / :data:`S3_DOWN`, #5012).  Opt-in, not
    generated, so the generated seeds and the paths they pin are unchanged."""
    return [replace(s, s3_read=fault) if s.kind == "presplit" else s for s in steps]


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


_GENERATED_RULING = re.compile(r"^Ruling (\S+-\d+)\. ")


def _generated_ruling(text: str | None) -> str | None:
    """The generated ruling a ruling text was made from (``<token>-<idx>``,
    see :func:`_text`), whatever the transcription appended (:func:`_cites`);
    the text itself when it is not a generated ruling's (a whole document)."""
    m = _GENERATED_RULING.match(text or "")
    return m.group(1) if m else text


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
    """What the worker's LLM extractors return (multimodal and text): the
    current step's rows, or ``ExtractionIncompleteError``, as a strict
    extraction whose API calls all failed raises (#4940, #4948).

    The worker builds its extractors itself (``LlmExtractor`` for real, see
    :class:`_RoutedExtractor`); their calls come here.  ``broken`` names
    the extractors (``framework`` / ``multimodal`` / ``county``) whose
    build fails, as a missing secret or a client init error makes it
    (#4953).
    """

    def __init__(self, events: Counter[str] | None = None) -> None:
        self.rulings: list[ExtractedRuling] = []
        #: Every call fails (``fail_mode == "all"``).
        self.failing = False
        #: The step's :data:`FAIL_MODES` entry, or ``""``.
        self.fail_mode = ""
        self.broken: set[str] = set()
        #: ``harness.call.<extractor>.<ok|fail>`` counts (#4968).
        self.events: Counter[str] = events if events is not None else Counter()

    def result(self, role: str, text: str | None = None, **kwargs: Any) -> list[ExtractedRuling]:
        if self.fail_mode == WRONG_SHAPE and not self.failing and role != "multimodal":
            return self._wrong_shape_result(role, text or "", **kwargs)
        if self.fail_mode == PARTIAL and not self.failing:
            # Every part but the last answered (#5000).
            from framework.llm_extractor import PartialExtractionError

            self.events[f"harness.call.{role}.partial"] += 1
            kept = [r.model_copy() for r in self.rulings[:-1]]
            raise PartialExtractionError(
                f"harness: one {role} part failed",
                rulings=kept,
                failed_parts=[len(self.rulings) - 1],
            )
        fails = self.failing or self.fail_mode == "all"
        if role == "multimodal" and self.fail_mode in (*FAIL_MODES, WRONG_SHAPE):
            fails = True
        self.events[f"harness.call.{role}.{'fail' if fails else 'ok'}"] += 1
        if fails:
            from framework.llm_extractor import ExtractionIncompleteError

            raise ExtractionIncompleteError(f"harness: every {role} API call failed")
        return [r.model_copy() for r in self.rulings]

    def _wrong_shape_result(self, role: str, text: str, **kwargs: Any) -> list[ExtractedRuling]:
        """The real ``LlmExtractor.extract`` on *text*, called as the worker
        called it (*kwargs*), against a model answering
        :data:`WRONG_SHAPE_RESPONSE` (#5011).  Whatever it returns or raises
        is the worker's to handle: a parser that took the answer for "no
        rulings" returns ``[]`` here, which the harness then sees replace
        the key's rows."""
        from framework.llm_extractor import ExtractionIncompleteError, LlmExtractor
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text=WRONG_SHAPE_RESPONSE, input_tokens=1, output_tokens=1, finish_reason="STOP"
        )
        with (
            patch("framework.llm_extractor._create_google_client", return_value=MagicMock()),
            patch("ingestion.llm_providers.call_llm", return_value=response),
        ):
            real = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
            real._cache = None
            try:
                rulings = real.extract(text, **kwargs)
            except ExtractionIncompleteError:
                self.events[f"harness.call.{role}.wrong_shape_failed"] += 1
                raise
        self.events[f"harness.call.{role}.wrong_shape_returned"] += 1
        return rulings


class _RoutedExtractor:
    """An extractor the worker built: its calls go to the harness's
    :class:`FakeExtractor` under *role*."""

    def __init__(self, fake: FakeExtractor, role: str) -> None:
        self._fake = fake
        self.role = role

    def extract_from_pdf(self, _pdf: bytes, **_kwargs: Any) -> list[ExtractedRuling]:
        return self._fake.result("multimodal")

    def extract(self, text: str, **kwargs: Any) -> list[ExtractedRuling]:
        return self._fake.result(self.role, text, **kwargs)


#: The worker method building an extractor, and the extractor it builds.
_BUILDERS = {
    "_get_framework_extractor": "framework",
    "_get_multimodal_extractor": "multimodal",
    "_get_county_extractor": "county",
}


def _extractor_factory(fake: FakeExtractor) -> Callable[..., _RoutedExtractor]:
    """Stands in for ``ingestion.worker.LlmExtractor``.

    Builds the real ``LlmExtractor`` first, so a missing or blank key fails
    exactly as in production (no LLM cache: ``JUDGEMIND_ARCHIVE_BUCKET`` is
    blank), then fails the build of an extractor in ``fake.broken``.  The
    worker method calling it says which extractor it is.
    """
    from framework.llm_extractor import LlmExtractor

    def build(**kwargs: Any) -> _RoutedExtractor:
        role = _BUILDERS.get(sys._getframe(1).f_code.co_name, "preflight")
        LlmExtractor(**kwargs)
        if role in fake.broken:
            raise ValueError(f"harness: the {role} extractor cannot be built")
        return _RoutedExtractor(fake, role)

    return build


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
            # A refusal names the incomplete reason it records (#5094).
            reason = getattr(record, "incomplete_reason", None)
            if isinstance(reason, str):
                self.counter[f"{event}:{reason}"] += 1


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
        self,
        dsn: str,
        run_token: str,
        seed: int,
        invariants: frozenset[str] | None = None,
        *,
        profile: str = "default",
        env: str = "google",
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
        #: ``telemetry_event`` values the code under test logged (#4929),
        #: and the harness's own ``harness.*`` counts (#4968).
        self.events: Counter[str] = Counter()
        self.extractor = FakeExtractor(self.events)
        self._key: Key | None = None
        self._step_label = ""
        self._snapshots: list[Counter[tuple[str, str]]] = []
        #: ``validation_results`` ids for the key seen committed this step.
        self._validation_seen: set[str] = set()
        self._pending: InvariantError | None = None
        self.conn = _CheckedConn(self.real, self._after_commit)
        #: alert id -> (s3_key, ruling identity)
        self._alerts: dict[str, tuple[str, tuple[str, str]]] = {}
        #: (identity before, identity now) of each alert whose ruling row
        #: changed identity in the last step (``alert_case``, #4946).
        self._alert_moves: list[tuple[tuple[str, str], tuple[str, str]]] = []
        self._user_sub: str | None = None
        #: Fields merged into every step's event (a deterministic splitter's
        #: county, format and text).
        self.event_extra: dict[str, Any] = {}
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
        #: A failing step's extractors cannot be built (#4953), rather than
        #: built and failing every call (#4948).
        self.fail_unavailable = False
        #: The next steps exhaust a split child's enrichment
        #: (:func:`run_enrichment_failure`): their split is incomplete, so
        #: ``marker`` expects an ``exhausted_child`` record (#5008).
        self.expect_exhausted = False
        #: The next whole-document steps exhaust the enrichment of their one
        #: child (:func:`run_whole_document_exhaustion`, #5035): the step
        #: raises ``LlmEnrichmentExhaustedError`` before any write, so the
        #: key is ``untouched`` and ``marker`` expects ``exhausted_child``.
        self.exhaust_whole = False
        #: S3 keys this harness has run a step on.
        self._keys: set[str] = set()
        #: Every judge a ruling on those keys has been on (``_check_votes``).
        self._judges_seen: set[str] = set()
        #: The archived raw object of each S3 key, which the worker's S3
        #: client returns (a pre-split child's page, #4976).
        self.raw_objects: dict[str, bytes] = {}
        #: The S3 read fault of the running ``presplit`` step
        #: (``Step.s3_read``, #5012), and whether its one flaky read failed.
        self._s3_fault = ""
        self._s3_flaked = False
        #: The run's county extraction config and env shape (#4968).
        self.profile = profile
        self.env = env
        #: Providers whose missing key :meth:`_check_refuses_start` checked.
        self._refusal_checked: set[str] = set()
        #: Whether the last step's empty split set was refused (#5057).
        self.last_refused = False
        #: Whether the current step's refusal was of an extraction's skip
        #: verdict (``empty_extraction``, #5094).
        self._empty_refusal = False
        #: Fresh keys :meth:`_check_refusal_rejects_fresh` has used.
        self._refusal_oracles = 0
        self._patches: list[Any] = []
        try:
            self._start_extraction_env()
            self.worker = self._make_worker()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._patches = []
        self._ingestion_logger.removeHandler(self._log_handler)
        self._ingestion_logger.setLevel(self._saved_log_level)
        self.real.close()
        self.checker.close()

    # -- extraction env (#4968) -----------------------------------------

    def _start_extraction_env(self) -> None:
        """Set the run's env shape and county configs, and route the
        worker's ``LlmExtractor`` to :class:`FakeExtractor`.  All undone by
        :meth:`close`."""
        from framework import extraction_config
        from ingestion import worker as worker_mod

        env = {
            # The deployed configuration (#4953): the suite's conftest opts
            # into ``optional`` mode, which only the extractor-less unit
            # tests need.
            "JUDGEMIND_EXTRACTION": "required",
            # No S3 LLM cache: building an extractor stays offline.
            "JUDGEMIND_ARCHIVE_BUCKET": "",
            **_ENV_VARS[self.env],
        }
        env_patch = patch.dict(os.environ, env)
        env_patch.start()
        self._patches.append(env_patch)
        if "ANTHROPIC_API_KEY" not in _ENV_VARS[self.env]:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        configs = patch.dict(extraction_config._COUNTY_CONFIGS)
        configs.start()
        self._patches.append(configs)
        config = profile_config(self.profile)
        if config is not None:
            # Named at startup, as a deployed county config is: the worker
            # builds an extractor for every provider the configs name.
            extraction_config._COUNTY_CONFIGS[("CA", "HARNESS PROFILE")] = config
        factory = patch.object(worker_mod, "LlmExtractor", _extractor_factory(self.extractor))
        factory.start()
        self._patches.append(factory)
        # The scripts' preflight (``rebuild_db.py``), then the worker.
        if worker_mod.check_extraction_ready() != worker_mod.EXTRACTION_REQUIRED:
            raise InvariantError("error", "check_extraction_ready did not run in required mode")

    def _configure_county(self, key: Key, profile: str) -> None:
        """Give *key*'s county the extraction config of *profile*."""
        from framework import extraction_config

        config = profile_config(profile)
        if config is not None:
            extraction_config._COUNTY_CONFIGS[("CA", key.county.upper())] = config

    # -- worker ---------------------------------------------------------

    def _new_worker(self) -> Any:
        """An ``IngestionWorker`` built by the production path: in
        ``required`` mode its constructor builds the extractors
        (``_require_extractors``) and raises when one cannot be built."""
        from ingestion import worker as worker_mod

        with patch.object(worker_mod, "create_llm_client", return_value=None):
            return worker_mod.IngestionWorker(
                redis_client=MagicMock(),
                pg_dsn=self.dsn,
                opensearch_client=MagicMock(),
                s3_client=MagicMock(),
                archive_bucket="harness-bucket",
            )

    def _make_worker(self) -> Any:
        worker = self._new_worker()
        if not worker._extraction_required:
            raise InvariantError("error", "the harness worker is not in required mode")
        worker._conn = self.real
        worker._get_connection = lambda: self.conn  # type: ignore[method-assign]
        worker._indexer = self.indexer
        worker._fetch_raw_pdf_from_s3 = lambda *_a, **_k: b"%PDF-1.4 harness"  # type: ignore[method-assign]

        def get_object(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 — boto3's names
            from botocore.exceptions import ClientError

            # A pre-split step's S3 fault (``Step.s3_read``, #5012).
            if self._s3_fault == S3_DOWN or (self._s3_fault == S3_FLAKY and not self._s3_flaked):
                self._s3_flaked = True
                self.events[f"harness.s3_fault.{self._s3_fault}"] += 1
                code = "ServiceUnavailable" if self._s3_fault == S3_DOWN else "SlowDown"
                raise ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")
            return {"Body": io.BytesIO(self.raw_objects[Key])}

        worker._s3_client.get_object.side_effect = get_object
        # The read's transient retries, without the waits.
        worker._PAGE_READ_RETRY_DELAYS_S = (0.0, 0.0, 0.0)
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

    def presplit_events(self, key: Key, step: Step) -> list[dict[str, Any]]:
        """The events a pre-split scraper capture of *step* emits (#4967).

        One per ruling its LLM returned with text, in page order, each with
        the fields filled and ``extra["pre_split"]``, as the Contra Costa and
        Los Angeles scrapers emit them (rows without text are dropped, as
        Contra Costa's ``kept_rulings``).  The id is the slot
        ``ScraperBase`` gives a pre-split child: ``split_child_document_id``
        of the content parent and the ruling's position.
        """
        from ingestion.split_ids import split_child_document_id

        rulings = [r for r in extracted_rulings(step) if r.ruling_text]
        # The page the scraper archived; its text is the step's doc.  A real
        # key names its content (its hash), so the worker caches a page per
        # key; the harness gives one key new bytes each step.
        self.raw_objects[key.s3_key] = step.doc.encode()
        self.worker._page_text_cache.clear()
        self._s3_fault = step.s3_read
        self._s3_flaked = False
        events = []
        for position, ruling in enumerate(rulings):
            event = self._event(key, "live")
            event.update(
                {
                    "document_id": split_child_document_id(key.parent, position, len(rulings)),
                    "content_format": "html",
                    "ruling_text": ruling.ruling_text,
                    "case_number": ruling.extracted_case_number,
                    "case_title": ruling.extracted_case_title,
                    "judge_name": ruling.extracted_judge_name,
                    "motion_type": ruling.motion_type,
                    "parties": [{"name": p.name, "role": p.role} for p in ruling.extracted_parties],
                    "extra": {
                        "_llm_extracted": True,
                        "pre_split": True,
                        "split_position": position,
                        "split_count": len(rulings),
                    },
                }
            )
            events.append(event)
        return events

    # -- running --------------------------------------------------------

    def run_step(self, key: Key, step: Step, label: str) -> None:
        """Run one step on *key*; raise :class:`InvariantError` on a
        broken invariant."""
        self._key = key
        step_started = self._db_now()
        self._keys.add(key.s3_key)
        self._step_label = f"seed={self.seed} {label} {step.describe()}"
        self._attach_alerts(key)
        self._ensure_roster(key, step.roster)
        before_rows = read_key(self.checker, key.s3_key)
        before = ruling_identities(before_rows)
        judges_before = self._judge_rows(key)
        spelling_before = dict(self._spelling)
        search_before = self._search_docs(key)
        doc = self._step_doc(step)
        marker_before = self._incomplete_marker(key)
        outcome = self.expected_outcome(step, has_rows=bool(before_rows))
        if self.exhaust_whole:
            if not step.whole or outcome != "ok":
                self._fail(
                    "error",
                    f"an exhausted whole-document step needs a whole step whose extraction "
                    f"is complete (whole={step.whole}, outcome={outcome}): the scenario "
                    "would prove nothing",
                )
            outcome = "exhausted"
        self.events[f"harness.outcome.{outcome}"] += 1
        complete = outcome in ("ok", "skipped")
        unavailable = outcome == "unavailable"
        for row in step.rows:
            if row.kind == "ruling" and not step.whole and complete:
                spec = step.specs[row.spec]
                self._spelling[spec.text] = spec.judge
        self._snapshots = []
        self._validation_seen = self._validation_ids(key)
        self._pending = None
        self._configure_county(key, step.profile)
        fail_mode = self._fail_mode(step)
        if fail_mode:
            self.events[f"harness.fail_mode.{fail_mode}.{outcome}"] += 1
        if fail_mode in ("framework_missing", "county_missing"):
            self._check_refuses_start(step, fail_mode)
        self.extractor.rulings = extracted_rulings(step)
        self.extractor.fail_mode = fail_mode
        self._break_extractors(step, fail_mode)
        # An incomplete extraction on a split-set replacement reports the
        # key as failed (#4948, #4968); a live capture does not raise.  A
        # missing extractor raises on every step kind (#4953).
        expect_failed = unavailable or (outcome == "failed" and step.kind != "live")
        # An exhausted enrichment raises on every step kind: a live worker
        # retries the message, a reingest counts the key failed (#5035).
        expect_failed = expect_failed or outcome == "exhausted"
        # An unreadable pre-split page fails closed: the child raises and
        # writes nothing (#5012).
        expect_failed = expect_failed or outcome == EVIDENCE_UNAVAILABLE
        if outcome == EVIDENCE_UNAVAILABLE:
            expected_error = "PreSplitEvidenceUnavailableError"
        elif outcome == "exhausted":
            expected_error = "LlmEnrichmentExhaustedError"
        elif unavailable:
            expected_error = "ExtractorUnavailableError"
        elif self._unextractable(step):
            # Nothing could be read: reported, never retried (#4983).
            expected_error = "NoExtractableTextError"
        else:
            expected_error = "SplitExtractionFailedError"
        reported_failed = False
        logged_before = self.events[_EXTRACTION_FAILED_EVENT]
        refused_before = self.events[_EMPTY_REFUSED_EVENT]
        empty_refused_before = self.events[_EMPTY_EXTRACTION_REFUSED_EVENT]
        try:
            with _real_enrichment(), self._exhausted_enrichment(outcome == "exhausted"):
                if step.kind == "db":
                    DB_MODE_DRIVER(self, key, step)
                elif step.kind == "presplit":
                    for event in self.presplit_events(key, step):
                        self.worker.process_event(event)
                else:
                    event = self._event(key, step.kind, step.doc)
                    with self._incomplete_input(key, event, fail_mode == INCOMPLETE_INPUT):
                        self.worker.process_event(event)
        except InvariantError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a violation
            self.events[f"harness.raised.{type(exc).__name__}"] += 1
            # A split-set replacement whose every child was rejected keeps
            # the key's rulings and reports the key failed (#5057); whether
            # the refusal was right is checked below (``refused``).
            # An extraction's skip verdict refused over the key's rulings
            # raises its subclass (#5094).
            empty_refusal = self.events[_EMPTY_EXTRACTION_REFUSED_EVENT] > empty_refused_before
            refused_raise = type(exc).__name__ == (
                "EmptyExtractionRefusedError" if empty_refusal else "AllChildrenRejectedError"
            ) and (self.events[_EMPTY_REFUSED_EVENT] > refused_before)
            if not (expect_failed and type(exc).__name__ == expected_error) and not refused_raise:
                self._fail("error", f"step raised {type(exc).__name__}: {exc}")
            reported_failed = True
        finally:
            self.extractor.fail_mode = ""
            self.extractor.broken = set()
            self._s3_fault = ""
            try:
                self.real.rollback()
            except Exception:  # noqa: BLE001
                pass
        if self._pending is not None:
            raise self._pending
        after_rows = read_key(self.checker, key.s3_key)
        after = ruling_identities(after_rows)
        # The worker refused an empty split set over the key's rulings
        # (#5057): nothing was written, so the step is checked as one that
        # wrote nothing (``untouched``, ``marker``), not as a complete one.
        refused = self.events[_EMPTY_REFUSED_EVENT] > refused_before
        # The refusal was of an extraction's skip verdict (#5094).
        self._empty_refusal = self.events[_EMPTY_EXTRACTION_REFUSED_EVENT] > empty_refused_before
        if refused:
            self.events["harness.refused"] += 1
            if self._empty_refusal:
                self.events["harness.refused.empty_extraction"] += 1
            self._guard(
                "refused",
                self._check_refused,
                step,
                before_rows,
                outcome,
                reported_failed,
            )
            self._spelling = spelling_before
            outcome = "rejected"
            complete = False
        # A live capture of a key with no rows yet writes the document on
        # its own when its extraction fails (``single_document_fallback``,
        # #4948), or the parts that answered when it was partial
        # (``partial_split_written``, #5000): the one failed step that is
        # not ``untouched``.  The following steps must converge from it
        # (#4970).
        fallback = outcome == "failed" and step.kind == "live" and not before_rows
        not_logged = self.events[_EXTRACTION_FAILED_EVENT] == logged_before
        if fallback and not_logged:
            self._fail("error", "a failed live capture of a new key did not take the fallback")
        if fallback and fail_mode == PARTIAL and after_rows:
            # The parts that answered were written as the key's split (#5000).
            self.events["harness.partial_new_key_written"] += 1
        if expect_failed and not reported_failed:
            self._guard("failed_manifest", self._check_reported, step, outcome)
        if not complete and not fallback:
            self._guard(
                "untouched",
                self._check_untouched,
                key,
                before_rows,
                after_rows,
                search_before,
                # An exhausted enrichment is no failed extraction: its raise
                # (``reported_failed``) shows the step took its branch.  A
                # refused empty split set logged its own event (#5057).
                not_logged and outcome not in ("exhausted", "rejected", EVIDENCE_UNAVAILABLE),
            )
        if step.kind == "presplit":
            # One event per ruling, each blind to its siblings (see the
            # module docstring): track the alerts, enforce only alert_case.
            try:
                self._check_alerts(key, after_rows)
            except InvariantError:
                pass
        else:
            self._guard("gap", self._check_gap, before, after)
            self._guard("alert", self._check_alerts, key, after_rows)
        self._guard("alert_case", self._check_alert_case)
        self._guard("search", self._check_search, key, after_rows)
        self._guard("search_fields", self._check_search_fields)
        self._guard("telemetry", self._check_telemetry, key)
        if step.kind != "presplit" and (
            outcome == "ok" or (outcome == "failed" and self._unextractable(step))
        ):
            # A complete step, or one nothing could read, is a verdict on
            # the key; a retryable failure is reported for a retry instead.
            self._guard("explained", self._check_explained, key, after_rows, step_started)
        if step.kind != "presplit" and outcome != "skipped":
            # A pre-split child is one ruling of its page, so it says
            # nothing about the key's completeness; a DB-row reingest of a
            # key with no rows selects nothing.
            self._guard(
                "marker",
                self._check_marker,
                key,
                step,
                outcome,
                fallback,
                reported_failed,
                marker_before,
                step_started,
            )
        if step.kind != "db" and not step.whole and complete:
            # A DB-row reingest only rewrites rows it selects, so it can
            # leave a slot untouched; the worker writes every ruling row.
            self._guard("synthetic_title", self._check_rows, key, after_rows, step)
        if complete:
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
        self._judges_seen.update(j for _d, _t, j, _n in judges_before + judges_after)
        self._guard("judge_converge", self._check_votes)
        if refused:
            # Last: it runs a step of its own on a fresh key.
            self._guard("refused", self._check_refusal_rejects_fresh, step, self._empty_refusal)
        self.last_refused = refused

    @contextmanager
    def _incomplete_input(self, key: Key, event: dict[str, Any], on: bool) -> Iterator[None]:
        """Make *event* an :data:`INCOMPLETE_INPUT` step's (#5010): its PDF
        has a scanned page, and every OCR call fails."""
        if not on:
            yield
            return
        from ingestion.llm_extract import SCANNED_PAGES_KEY

        pdf = blank_page_pdf()
        self.raw_objects[key.s3_key] = pdf
        if event.get("_replace_split_set"):
            event["ruling_text"] = pdf.decode("latin-1")
        else:
            # The scraper read the text layer and named the scanned page.
            event["extra"] = {**(event.get("extra") or {}), SCANNED_PAGES_KEY: [2]}
        self.events["harness.incomplete_input"] += 1
        with patch("ingestion.llm_extract.call_llm_with_images", return_value=None):
            yield

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

    def _check_reported(self, step: Step, outcome: str) -> None:
        """Called when an incomplete extraction raised nothing."""
        if outcome == EVIDENCE_UNAVAILABLE:
            self._fail(
                "failed_manifest",
                "a pre-split child whose archived page could not be read raised no "
                "PreSplitEvidenceUnavailableError, so its LLM case number went on "
                "without evidence (#5012)",
            )
        if outcome == "exhausted":
            self._fail(
                "failed_manifest",
                f"a {step.kind} step whose whole-document enrichment was exhausted raised "
                "no LlmEnrichmentExhaustedError, so neither the live worker's retry nor "
                "the reingest's failed-keys manifest sees it",
            )
        if outcome == "unavailable":
            self._fail(
                "failed_manifest",
                "a step whose extractor is unavailable raised no ExtractorUnavailableError, "
                "so the key is not reported as failed (a live worker would acknowledge "
                "the message instead of restarting)",
            )
        self._fail(
            "failed_manifest",
            f"an incomplete extraction on a {step.kind} step did not report the key as "
            "failed (no SplitExtractionFailedError), so the reingest's failed-keys "
            "manifest would not retry it",
        )

    # -- extraction outcome (#4968) -------------------------------------

    def _step_doc(self, step: Step) -> str:
        """The document text the step's event carries."""
        if step.kind in ("presplit", "db"):
            return step.doc
        return self.event_extra.get("ruling_text", step.doc)

    def _fail_mode(self, step: Step) -> str:
        if step.fail_mode:
            return step.fail_mode
        return "all" if step.fail and not self.fail_unavailable else ""

    def expected_outcome(self, step: Step, *, has_rows: bool) -> str:
        """What the step's extraction comes to, from the step alone.

        ``ok``: a complete extraction (rows, a whole document, or a verdict
        of no rulings).  ``failed``: no complete result (a call failed with
        nothing to fall back on, or nothing could be read at all).
        ``unavailable``: an extractor the step needs cannot be built.
        ``skipped``: a DB-row reingest of a key with no rows selects nothing.
        """
        from framework.extraction_config import ExtractionMethod

        if step.kind == "db" and not has_rows:
            return "skipped"
        if step.kind == "presplit":
            if step.s3_read == S3_DOWN and any(r.ruling_text for r in extracted_rulings(step)):
                # The archived page cannot be read: no child is written (#5012).
                return EVIDENCE_UNAVAILABLE
            return "ok"  # the scraper extracted the fields
        config = profile_config(step.profile)
        if config is not None and config.method == ExtractionMethod.NONE:
            return "ok"  # the scraper's fields are the document's
        mode = self._fail_mode(step)
        all_missing = step.fail and self.fail_unavailable
        if config is None or config.method == ExtractionMethod.MULTIMODAL:
            # The worker reads the page images (the harness's PDF bytes).
            if all_missing:
                return "unavailable"
            if mode == PARTIAL:
                # No text fallback after a partial page split (#5000).
                return "failed"
            if mode not in (*FAIL_MODES, WRONG_SHAPE):
                return "ok"
        if not self._step_doc(step):
            return "failed"  # no text to fall back on, or to read at all
        needed = "county" if config is not None and config.provider else "framework"
        if all_missing or mode == f"{needed}_missing":
            return "unavailable"
        # Incomplete input: the text lacks a scanned page (#5010).  A
        # malformed response is a failed chunk, not "no rulings" (#5011).
        return "failed" if mode in ("all", PARTIAL, INCOMPLETE_INPUT, WRONG_SHAPE) else "ok"

    def _unextractable(self, step: Step) -> bool:
        """A text-first county's step with no text: no extractor can run,
        so the failure is ``NoExtractableTextError`` (#4968, #4983)."""
        from framework.extraction_config import ExtractionMethod

        config = profile_config(step.profile)
        return (
            config is not None
            and config.method == ExtractionMethod.LLM
            and not self._step_doc(step)
        )

    def _break_extractors(self, step: Step, mode: str) -> None:
        """Make the extractors the step lacks unbuildable, and drop the
        worker's cached ones, so the worker's getters build them again and
        fail as an init error makes them."""
        broken: set[str] = set()
        if step.fail and self.fail_unavailable:
            broken = {"framework", "multimodal", "county"}
        elif mode in ("framework_missing", "county_missing"):
            broken = {mode.removesuffix("_missing")}
        self.extractor.broken = broken
        if "framework" in broken:
            self.worker._framework_extractor = None
        if "multimodal" in broken:
            self.worker._multimodal_extractor = None
            self.worker._multimodal_extractors.clear()
        if "county" in broken:
            self.worker._county_extractors.clear()

    def _check_refuses_start(self, step: Step, mode: str) -> None:
        """A worker missing the secret of the extractor *mode* lacks
        refuses to start, and the scripts' preflight refuses too (#4953,
        #4957)."""
        from ingestion import worker as worker_mod

        config = profile_config(step.profile)
        provider = worker_mod.framework_llm_provider()
        if mode == "county_missing" and config is not None and config.provider:
            provider = config.provider
        if provider in self._refusal_checked:
            return
        self._refusal_checked.add(provider)
        secret = "ANTHROPIC_API_KEY" if provider == "anthropic" else "GOOGLE_API_KEY"
        with patch.dict(os.environ):
            os.environ.pop(secret, None)
            for what, start in (
                ("check_extraction_ready", worker_mod.check_extraction_ready),
                ("the worker", self._new_worker),
            ):
                try:
                    start()
                except worker_mod.ExtractorUnavailableError:
                    self.events[f"harness.refused_start.{provider}"] += 1
                    continue
                self._guard(
                    "refuse_start",
                    self._fail,
                    "refuse_start",
                    f"{what} started without {secret} ({provider} extractors required)",
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

    def _db_now(self) -> datetime:
        with self.checker.cursor() as cur:
            cur.execute("SELECT clock_timestamp()")
            row = cur.fetchone()
        assert row is not None
        return row[0]

    # -- invariants -----------------------------------------------------

    def _check_explained(self, key: Key, after_rows: list[DocRow], since: datetime) -> None:
        """A step that leaves its key with no document rows records why
        (#5015): an ``ingest_skips`` row, a ``validation_results`` row, or the
        all-NULL guard's ``data_quality_metrics`` row for the key, written by
        this step.  Without one the raw-key reconciler cannot tell the
        deliberate non-write from a lost event."""
        if after_rows:
            return
        params = {"k": key.s3_key, "t": since}
        found = None
        with self.checker.cursor() as cur:
            for sql in (
                "SELECT 'ingest_skips:' || reason FROM telemetry.ingest_skips "
                "WHERE s3_key = %(k)s AND last_recorded_at >= %(t)s",
                "SELECT 'validation_results' FROM validation_results "
                "WHERE s3_key = %(k)s AND created_at >= %(t)s",
                "SELECT 'data_quality_metrics' FROM data_quality_metrics "
                "WHERE metric_name = 'non_ruling_pdf_dropped' "
                "AND metadata->>'s3_key' = %(k)s AND recorded_at >= %(t)s",
            ):
                cur.execute(sql + " LIMIT 1", params)
                found = cur.fetchone()
                if found is not None:
                    break
        if found is None:
            self._fail(
                "explained",
                "the step left the key with no document rows and recorded no reason "
                "(no ingest_skips, validation_results or non_ruling_pdf_dropped row)",
            )
        self.events[f"harness.explained.{found[0]}"] += 1
        skip = self._skip_record(key)
        if skip is not None and found[0] == f"ingest_skips:{skip[0]}":
            self._check_extraction_skip(skip[0], skip[1])

    def _skip_record(self, key: Key) -> tuple[str, dict[str, Any]] | None:
        """The key's ``telemetry.ingest_skips`` row: ``(reason, detail)``."""
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT reason, detail FROM telemetry.ingest_skips WHERE s3_key = %s",
                (key.s3_key,),
            )
            row = cur.fetchone()
        return None if row is None else (row[0], row[1] or {})

    def _check_extraction_skip(self, reason: str, detail: dict[str, Any]) -> None:
        """An extraction's skip verdict is re-judgeable and hid no ruling
        (#5094): it carries the current rule version, and it never replaced
        rulings the key held unless the run allowed the removal."""
        from validation.skip_versions import EXTRACTION_SKIP_RULE_VERSION, stale_skip_reason

        if reason not in _EXTRACTION_SKIPS:
            return
        if detail.get("rule_version") != EXTRACTION_SKIP_RULE_VERSION:
            self._fail(
                "explained",
                f"the {reason} skip carries rule version {detail.get('rule_version')!r}, "
                f"not {EXTRACTION_SKIP_RULE_VERSION}: the reconciler cannot re-judge it",
            )
        stale = stale_skip_reason(reason, detail)
        if stale is not None:
            self._fail(
                "explained",
                f"the {reason} skip replaced {detail.get('held_rulings')} ruling(s) the "
                f"key held (stale: {stale}); the worker must refuse it (#5094)",
            )
        self.events[f"harness.explained.extraction_skip.{reason}"] += 1

    def _incomplete_marker(self, key: Key) -> tuple[str, int, datetime] | None:
        """The key's ``telemetry.ingest_incomplete`` record (#5008):
        ``(reason, occurrences, last_recorded_at)``, or None."""
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT reason, occurrences, last_recorded_at "
                "FROM telemetry.ingest_incomplete WHERE s3_key = %s",
                (key.s3_key,),
            )
            row = cur.fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def _check_marker(
        self,
        key: Key,
        step: Step,
        outcome: str,
        fallback: bool,
        reported_failed: bool,
        before: tuple[str, int, datetime] | None,
        since: datetime,
    ) -> None:
        """A step that ends incomplete records it durably, and a complete
        step clears the record (#5008).  Without the record nothing
        re-splits the key: ``derived.documents`` has no completeness
        column, and a reingest's failed manifest expires."""
        after = self._incomplete_marker(key)
        recorded = after is not None and after[2] >= since

        def expect(reasons: tuple[str, ...], what: str) -> None:
            if not recorded or after is None or after[0] not in reasons:
                self._fail(
                    "marker",
                    f"{what} left no durable incomplete record for the key "
                    f"(want {'/'.join(reasons)}, found {after and after[0]!r}"
                    f"{'' if recorded else ' (not written by this step)'})",
                )
            self.events[f"harness.marker.{after[0]}"] += 1

        if self.expect_exhausted:
            expect((_MARKER_EXHAUSTED_CHILD,), "a split with an exhausted child")
            return
        if outcome == "exhausted":
            expect(
                (_MARKER_EXHAUSTED_CHILD,),
                f"a whole-document {step.kind} step whose enrichment was exhausted",
            )
            return
        if outcome == "rejected" and self._empty_refusal:
            expect(
                (_MARKER_EMPTY_EXTRACTION,),
                f"a {step.kind} step whose extraction's skip verdict was refused (#5094)",
            )
            return
        if outcome == "rejected":
            expect(
                (_MARKER_ALL_CHILDREN_REJECTED,),
                f"a {step.kind} step refused for rejecting every child (#5057)",
            )
            return
        if fallback:
            expect(_MARKER_FALLBACK_REASONS, "a live capture of a new key that took the fallback")
            return
        if outcome == "ok" or (
            outcome == "failed" and self._unextractable(step) and step.kind != "live"
        ):
            # A complete split, or a verdict that nothing is readable.
            if after is not None:
                self._fail(
                    "marker",
                    f"a complete {step.kind} step left the key's incomplete record "
                    f"({after[0]!r}), so the reconciler would re-split a complete key",
                )
            if before is not None:
                self.events["harness.marker.cleared"] += 1
            return
        if outcome == "failed" and reported_failed:
            expect((_MARKER_REINGEST_FAILED,), f"a {step.kind} step that reported the key failed")
            return
        # Nothing was written for another reason: a live capture of a key
        # that holds rows, or an extractor that is unavailable.
        if after != before:
            self._fail(
                "marker",
                f"a {step.kind} step that wrote nothing changed the key's incomplete "
                f"record: before {before}, after {after}",
            )

    def _check_refused(
        self, step: Step, before_rows: list[DocRow], outcome: str, reported_failed: bool
    ) -> None:
        """A refused empty split set (#5057) refused a complete extraction
        over a key that held rulings, and a split-set replacement reported
        the key failed (``AllChildrenRejectedError``)."""
        if outcome != "ok":
            self._fail(
                "refused",
                f"the worker refused an empty split set on a step whose extraction "
                f"was {outcome}, not complete",
            )
        if step.kind == "presplit":
            self._fail("refused", "a pre-split child's write was refused as an empty split set")
        if not any(r.ruling_id for r in before_rows):
            self._fail(
                "refused",
                "the worker refused an empty split set on a key that held no ruling",
            )
        if step.kind != "live" and not reported_failed:
            self._fail(
                "refused",
                f"a refused {step.kind} step raised no AllChildrenRejectedError, so the "
                "reingest does not count the key failed",
            )
        if self._empty_refusal and any(r.ruling_text for r in extracted_rulings(step)):
            # An ``empty_extraction`` refusal (#5094) is of a verdict that
            # the document holds no rulings: every row textless, or none.
            self._fail(
                "refused",
                "the worker refused an extraction's skip verdict on a step whose "
                "extraction returned a ruling with text",
            )

    def _check_refusal_rejects_fresh(self, step: Step, empty_refusal: bool = False) -> None:
        """The refusal's oracle (#5057): the same bytes ingested on a fresh
        key (no history) write no ruling either.  Otherwise the refused
        step did not reject every child on its merits.

        For an extraction's skip verdict (*empty_refusal*, #5094) the fresh
        key records the verdict as a skip, as before the fix, stamped with
        the current rule version and no held rulings, so the reconciler
        counts it as explained (and re-judges it once the rules change)."""
        self._refusal_oracles += 1
        owner = self._key.content_hash[:16] if self._key is not None else ""
        fresh = make_key(f"{self.run_token}-{self.seed}-{owner}-refusal-{self._refusal_oracles}")
        oracle = replace(step, kind="reingest", fail_mode="", fail=False)
        label = self._step_label
        self.run_step(fresh, oracle, f"refusal oracle {self._refusal_oracles}")
        self._step_label = label
        written = [r for r in read_key(self.checker, fresh.s3_key) if r.ruling_id]
        if written:
            self._fail(
                "refused",
                f"the worker refused an empty split set, but a fresh ingest of the same "
                f"bytes writes {len(written)} ruling(s)",
            )
        if empty_refusal:
            skip = self._skip_record(fresh)
            if skip is None or skip[0] not in _EXTRACTION_SKIPS:
                self._fail(
                    "refused",
                    "the worker refused an extraction's skip verdict, but a fresh ingest "
                    f"of the same bytes recorded no such skip (found {skip and skip[0]!r})",
                )
            self.events[f"harness.refused.fresh_skip.{skip[0]}"] += 1

    @contextmanager
    def _exhausted_enrichment(self, on: bool) -> Iterator[None]:
        """With *on*, every per-field enrichment call of the step is
        exhausted (``_llm_enrich_fields`` returns ``errored``, as after its
        retries, #5035).  Counts the calls, so a scenario that never reached
        the enrichment fails instead of passing vacuously.

        The whole-document child reaches its enrichment as in production: its
        per-field LLM extraction (``extract_fields_llm``) reads a case title,
        so the child is no bad-split fragment, and enrichment is asked for
        the fields still missing."""
        if not on:
            yield
            return
        from ingestion import worker as worker_mod
        from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

        def exhausted(_ruling_text: str, _document_id: str) -> tuple[Any, str]:
            self.events["harness.enrichment_exhausted"] += 1
            return None, "errored"

        def per_field(**_kwargs: Any) -> LLMExtractionResult:
            return LLMExtractionResult(
                case_count=1, rulings=[LLMRulingResult(case_title="Harness v. Exhausted")]
            )

        with (
            patch.object(self.worker, "_llm_enrich_fields", exhausted),
            patch.object(self.worker, "_llm_client", MagicMock()),
            patch.object(worker_mod, "extract_fields_llm", per_field),
        ):
            yield

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

        A reingest and a live step both move a ruling off a number its
        document lacks (#4915, #5006).  A scraper's pre-split child keeps an
        established case link (preserve-first, #2475, #4788): a ruling that
        kept its row and its case on a ``presplit`` step is exempt.
        """
        if not doc_is_evidence(doc):
            return
        held = {(r.ruling_id, r.case_number) for r in before_rows if r.ruling_id}
        for r in after_rows:
            number = r.case_number
            if not r.ruling_id or not number or number.startswith("UNKNOWN-"):
                continue
            if step.kind == "presplit" and (r.ruling_id, number) in held:
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
            # The judge's documents on the court's other keys vote too (#4969).
            printing |= {
                d
                for d, t, j, _n in self._judge_rows_elsewhere()
                if j == judge_id and key_of(self._spelling.get(t)) == target
            }
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

    def _check_votes(self) -> None:
        """Each document votes exactly as it stands, and every judge's name
        is what its votes give it (#4966).

        On every key the harness wrote: a ``judge_name_evidence`` row exists
        for each active document whose ruling has a judge, with that judge,
        and for no other document (deleted, superseded, retired, or whose
        ruling has no judge or another one).  And every judge a ruling on
        those keys has been on, including one whose rulings all moved away,
        keeps the name arbitration over its votes picks now:
        ``_choose_canonical_spelling`` returns the current name, so a
        recount from scratch would not rename it.  A judge that lost votes
        and was not re-arbitrated keeps a name its evidence no longer
        supports.
        """
        keys = sorted(self._keys)
        with self.checker.cursor() as cur:
            cur.execute(
                """
                SELECT e.document_id::text, e.judge_id::text, d.status::text, r.judge_id::text
                FROM judge_name_evidence e
                LEFT JOIN documents d ON d.id = e.document_id
                LEFT JOIN rulings r ON r.document_id = e.document_id
                WHERE (d.s3_key = ANY(%s) OR d.id IS NULL)
                  AND (d.status IS DISTINCT FROM 'active' OR r.judge_id IS DISTINCT FROM e.judge_id)
                  AND e.judge_id = ANY(%s::uuid[])
                """,
                (keys, sorted(self._judges_seen)),
            )
            stale = cur.fetchall()
            cur.execute(
                """
                SELECT d.id::text, r.judge_id::text
                FROM documents d JOIN rulings r ON r.document_id = d.id
                WHERE d.s3_key = ANY(%s) AND d.status = 'active' AND r.judge_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM judge_name_evidence e
                      WHERE e.document_id = d.id AND e.judge_id = r.judge_id
                  )
                """,
                (keys,),
            )
            missing = cur.fetchall()
        if stale:
            doc, judge, status, ruling_judge = stale[0]
            self._fail(
                "judge_converge",
                f"{len(stale)} stale judge vote(s): document {doc[:8]} votes for judge "
                f"{judge[:8]} but is {status or 'deleted'} with its ruling on "
                f"{(ruling_judge or 'no judge')[:8]}",
            )
        if missing:
            doc, judge = missing[0]
            self._fail(
                "judge_converge",
                f"{len(missing)} active document(s) with a judge cast no vote: document "
                f"{doc[:8]}, judge {judge[:8]}",
            )
        if not self._judges_seen:
            return
        with self.checker.cursor() as cur:
            cur.execute(
                """
                SELECT j.id::text, j.canonical_name, j.court_id::text, e.raw_name, COUNT(e.*)
                FROM judges j
                LEFT JOIN (
                    judge_name_evidence e
                    JOIN documents d ON d.id = e.document_id AND d.status = 'active'
                    JOIN rulings r ON r.document_id = e.document_id AND r.judge_id = e.judge_id
                ) ON e.judge_id = j.id
                WHERE j.id = ANY(%s::uuid[])
                GROUP BY j.id, j.canonical_name, j.court_id, e.raw_name
                """,
                (sorted(self._judges_seen),),
            )
            judges: dict[str, tuple[str, str, list[tuple[str, int]]]] = {}
            for judge_id, name, court_id, raw, count in cur.fetchall():
                entry = judges.setdefault(judge_id, (name, court_id, []))
                if raw is not None:
                    entry[2].append((raw, int(count)))
            for judge_id, (name, court_id, evidence) in sorted(judges.items()):
                winner, docs = _choose_canonical_spelling(name, evidence)
                if winner.lower() == name.lower():
                    continue
                cur.execute(
                    "SELECT 1 FROM judges WHERE court_id = %s::uuid AND canonical_name = %s "
                    "AND id <> %s::uuid",
                    (court_id, winner, judge_id),
                )
                if cur.fetchone() is not None:
                    continue  # another judge holds the name; arbitration keeps it
                self._fail(
                    "judge_converge",
                    f"judge {name!r} keeps a name its votes no longer give it: they pick "
                    f"{winner!r} ({docs})",
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
        self._alert_moves = []
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
                if now != identity:
                    self._alert_moves.append((identity, now))
                self._alerts[alert_id] = (s3_key, now)
        for identity, count in sorted(recorded.items()):
            if attached[identity] < min(present[identity], count):
                self._fail(
                    "alert",
                    f"alert on ruling {(identity[0] or '')[:24]!r} ({identity[1]}) lost "
                    "its ruling_id while the ruling still exists",
                )

    def _check_alert_case(self) -> None:
        """An alert follows only its own ruling (#4946).

        A ruling row whose alert carried over from the last step must hold
        the same ruling: the same generated ruling (its case number was
        re-extracted or relinked, or its transcription now cites a
        different number), or the same real case (its text was
        re-extracted).  A synthetic ``UNKNOWN`` case is evidence of a slot,
        not of a ruling, so another ruling there is a different ruling: the
        old row is retired and its alert detached.  Another ruling on
        another case is a different ruling about a different case.
        """
        for (old_text, old_case), (new_text, new_case) in self._alert_moves:
            if _generated_ruling(old_text) == _generated_ruling(new_text):
                continue
            if old_case == new_case and old_case != "UNKNOWN":
                continue
            self._fail(
                "alert_case",
                f"alert on ruling {(old_text or '')[:24]!r} ({old_case}) now points at "
                f"a different ruling {(new_text or '')[:24]!r} ({new_case})",
            )

    # -- several keys share one court (#4969) ---------------------------

    def _held_on(self, keys: list[Key]) -> dict[str, set[str]]:
        """``{ruling text: the S3 keys of the active documents holding it}``."""
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT d.s3_key, r.ruling_text FROM rulings r "
                "JOIN documents d ON d.id = r.document_id "
                "WHERE d.status = 'active' AND d.s3_key = ANY(%s)",
                ([k.s3_key for k in keys],),
            )
            held: dict[str, set[str]] = {}
            for s3_key, text in cur.fetchall():
                held.setdefault(text or "", set()).add(s3_key)
        return held

    def _check_printed_somewhere(self, keys: list[Key], printed: dict[str, set[str]]) -> None:
        """Every ruling a key's last extraction returned (*printed*, by S3
        key) is held by an active document of a key that lists it (#4969).

        A repost loses content-hash dedup to the key that holds the ruling,
        so its document is superseded and holds nothing.  When the holder
        stops listing the ruling, a key that still lists it must hold it.
        """
        held = self._held_on(keys)
        label = {k.s3_key: f"k{n}" for n, k in enumerate(keys)}
        for text in sorted(set().union(*printed.values())):
            listing = {s3_key for s3_key, texts in printed.items() if text in texts}
            if not held.get(text, set()) & listing:
                self._fail(
                    "printed_somewhere",
                    f"ruling {text[:24]!r}, which {sorted(label[k] for k in listing)} list, "
                    f"is held by no active document of those keys (held on "
                    f"{sorted(label[k] for k in held.get(text, ()))})",
                )

    def _check_held_printed(self, keys: list[Key], printed: dict[str, set[str]]) -> None:
        """No key holds a ruling its last extraction did not return (#4969)."""
        label = {k.s3_key: f"k{n}" for n, k in enumerate(keys)}
        for text, where in sorted(self._held_on(keys).items()):
            for s3_key in sorted(where):
                if s3_key in printed and text not in printed[s3_key]:
                    self._fail(
                        "held_printed",
                        f"{label[s3_key]} holds ruling {text[:24]!r}, which its last "
                        "extraction did not return",
                    )

    def _judge_rows_elsewhere(self) -> list[tuple[str, str, str, str]]:
        """:meth:`_judge_rows` of every key this harness wrote other than
        the current step's.  The step leaves those rows as they were."""
        others = sorted(self._keys - {self._key.s3_key} if self._key else self._keys)
        if not others:
            return []
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT d.id::text, r.ruling_text, j.id::text, j.canonical_name "
                "FROM rulings r JOIN documents d ON d.id = r.document_id "
                "JOIN judges j ON j.id = r.judge_id WHERE d.s3_key = ANY(%s)",
                (others,),
            )
            return [tuple(row) for row in cur.fetchall()]

    def _check_one_court_judge(self, keys: list[Key]) -> None:
        """``judge_converge`` across every key of the court: one roster
        judge's rulings resolve to one judge row, whichever key holds them,
        and each active document's vote is the spelling it prints now."""
        self._check_one_judge(keys[0], [row for key in keys for row in self._judge_rows(key)])
        with self.checker.cursor() as cur:
            cur.execute(
                "SELECT d.id::text, r.ruling_text, e.raw_name FROM documents d "
                "JOIN rulings r ON r.document_id = d.id "
                "JOIN judge_name_evidence e ON e.document_id = d.id AND e.judge_id = r.judge_id "
                "WHERE d.s3_key = ANY(%s) AND d.status = 'active'",
                ([k.s3_key for k in keys],),
            )
            votes = cur.fetchall()
        for doc, text, raw in votes:
            want = self._spelling.get(text)
            if want and _spelled(raw) != _spelled(want):
                self._fail(
                    "judge_converge",
                    f"document {doc[:8]} votes for spelling {raw!r}, its ruling prints {want!r}",
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
    extraction_modes: bool | None = None,
) -> Counter[str]:
    """Run *seed*'s sequence and the convergence checks; raise
    :class:`InvariantError` on the first broken invariant.  Two runs of one
    seed in one session need distinct *name*s (their keys derive from it).
    *invariants* limits the check to those invariants (default: all);
    *whole_documents* and *extraction_modes* are :func:`generate`'s.
    Returns the ``telemetry_event`` counts the run logged."""
    steps = generate(seed, whole_documents=whole_documents, extraction_modes=extraction_modes)
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
    harness = KeyHarness(
        dsn, run_token, seed, invariants, profile=steps[0].profile, env=steps[0].env
    )
    try:
        key = make_key(f"{run_token}-{seed}-{name}-main")
        if setup is not None:
            setup(harness, key)
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")

        # The convergence checks extract the final bytes healthily: a failed
        # extraction's result is no result (#4968).
        last = steps[-1]
        final = replace(last, fail_mode="", fail=False, s3_read="")
        no_rows = not read_key(harness.checker, key.s3_key)
        if final.kind in ("live", "presplit") or (final.kind == "db" and no_rows) or final != last:
            # A live write keeps an established case link (preserve-first,
            # #2475), so its result may depend on history by design, and a
            # DB-row reingest only re-ingests keys that have rows.  A
            # split-set replacement must not depend on history: re-ingest
            # the same bytes.  A failed step left the key as it was.
            settle = replace(final, kind="reingest", ops=("settle",))
            harness.run_step(key, settle, "settle")
        # The final bytes reject every child, so the key keeps the rulings
        # its history left (#5057); the refusal's oracle already checked
        # that a fresh ingest writes none.
        kept_by_refusal = harness.last_refused
        history = normalized(key, read_key(harness.checker, key.s3_key))
        # Bytes nothing can be extracted from (no text in a text-first
        # county) leave a key as its history left it, in the failed-keys
        # manifest; only a fresh live capture and a fresh reingest of them
        # must agree.
        extractable = harness.expected_outcome(replace(final, kind="reingest"), has_rows=True)

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
        if (
            extractable == "ok"
            and not kept_by_refusal
            and not _converged(history, fresh["reingest"], near_miss_ok=near_miss_ok)
        ):
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
) -> Counter[str]:
    """Ingest a key through *splitter* with enrichment healthy, then re-ingest
    it while enrichment is exhausted for one random child (or, sometimes,
    every child).  Each failed child must keep the document and ruling its
    slot held (``kept``), the step must record the key incomplete
    (``marker``, #5008), and it must hold the other invariants.  Returns the
    run's event counts."""
    from ingestion.split_ids import split_child_document_id

    rng = random.Random(seed)
    token = f"{run_token}-{splitter.name}-{seed}"
    n_specs = rng.randint(2, 6)
    specs = [
        Spec(
            idx=i,
            case_number=_county_case_number(splitter.county, i, token),
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
            harness.expect_exhausted = True
            try:
                harness.run_step(key, second, "enrichment exhausted")
            finally:
                harness.expect_exhausted = False
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
    return harness.events


def run_whole_document_exhaustion(
    dsn: str, seed: int, run_token: str, *, kind: str, name: str = "whole-exhausted"
) -> Counter[str]:
    """*seed*'s generated history on a key, then a *kind* step on which the
    split finds no rulings and the whole document's one child exhausts its
    per-field enrichment, then a healthy reingest of the same whole
    document (#5035).

    The exhausted step raises ``LlmEnrichmentExhaustedError`` before any
    write, so it must leave the key ``untouched`` and record it
    ``exhausted_child`` (``marker``), on a split-set replacement as on a live
    capture; the healthy reingest must clear the record.  Before the fix only
    a live capture's dead-letter recorded the key, and a reingest left it
    only in the failed manifest under ``tmp/keys/``, which expires.  Returns
    the run's event counts.
    """
    steps = generate(seed, extraction_modes=False)
    harness = KeyHarness(dsn, run_token, seed)
    try:
        key = make_key(f"{run_token}-{seed}-{name}-{kind}")
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")
        # A document with no text has nothing to enrich: give it the few
        # characters of a scanned PDF's text layer.
        doc = steps[-1].doc or "harness scanned page"
        whole = replace(steps[-1], whole=True, fail=False, fail_mode="", doc=doc)
        if kind == "db" and not read_key(harness.checker, key.s3_key):
            # A DB-row reingest selects only keys that have rows.
            harness.events["harness.whole_exhausted.db_without_rows"] += 1
            kind = "reingest"
        exhausted = replace(whole, kind=kind, ops=("whole", "exhausted"))
        harness.exhaust_whole = True
        try:
            harness.run_step(key, exhausted, "whole-document enrichment exhausted")
        finally:
            harness.exhaust_whole = False
        if not harness.events["harness.enrichment_exhausted"]:
            raise InvariantError(
                "error",
                f"seed={seed} {exhausted.describe()}: the whole-document child never "
                "reached its enrichment (the scenario would prove nothing)",
            )
        healthy = replace(whole, kind="reingest", ops=("whole", "healthy"))
        harness.run_step(key, healthy, "whole-document reingest")
    finally:
        harness.close()
    return harness.events


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
    unavailable: bool = False,
) -> None:
    """Run a history on a key (*steps*, default *seed*'s generated
    sequence), then re-ingest it while every extraction call fails
    (``ExtractionIncompleteError``), or, with *unavailable*, while the
    worker's required extractors cannot be built (#4953).  The failed step
    must leave the key's rows and search docs exactly as they were
    (``untouched``) and hold the other invariants; a split-set replacement
    must report the key as failed, and so must every step kind when the
    extractors are unavailable.  Raises :class:`InvariantError` on a broken
    invariant.

    The failed step is *kind*, or one the seed picks: a prefix reingest, a
    DB-row reingest or a live capture (the last two only when the key
    holds rows; with *unavailable*, a live capture of a key with no rows
    too).
    """
    # The extraction modes have their own failed steps; this history is
    # the one these tests were written against.
    steps = steps if steps is not None else generate(seed, extraction_modes=False)
    rng = random.Random(f"{seed}:fail")
    harness = KeyHarness(dsn, run_token, seed)
    try:
        key = make_key(f"{run_token}-{seed}-{name}")
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")
        if kind is None:
            has_rows = bool(read_key(harness.checker, key.s3_key))
            if has_rows:
                kind = rng.choice(("reingest", "db", "live"))
            else:
                kind = rng.choice(("reingest", "live") if unavailable else ("reingest",))
        harness.fail_unavailable = unavailable
        failed = replace(steps[-1], kind=kind, fail=True, whole=False)
        harness.run_step(key, failed, "extraction failed")
    finally:
        harness.close()


def run_new_key_fallback(
    dsn: str, seed: int, run_token: str, *, name: str = "fallback"
) -> Counter[str]:
    """A live capture of a new key whose extraction fails, then *seed*'s
    generated sequence (#4970).

    The failed capture writes the document on its own, on the parent row
    (``single_document_fallback``, ``_write_one_ruling``).  The sequence
    that follows must hold every invariant and converge: its first split
    replaces the parent row with the split's children.  Raises
    :class:`InvariantError` on a broken invariant.  Returns the run's event
    counts.
    """
    steps = generate(seed, extraction_modes=False)
    first = replace(steps[0], kind="live", fail=True, whole=False, ops=("fallback",))
    return run_steps(dsn, [first, *steps], run_token, seed=seed, name=name)


def run_partial_extraction(
    dsn: str, seed: int, run_token: str, *, kind: str, name: str = "partial"
) -> Counter[str]:
    """A partial extraction (:data:`PARTIAL`, #5000) on *seed*'s key.

    *kind* ``new-live``: a live capture of a new key is partial, so it
    writes the parts that answered; *seed*'s sequence follows and must hold
    every invariant and converge from those rows.  Otherwise *seed*'s
    history runs, then a partial step of *kind* (``reingest``, ``db`` or
    ``live``): it must leave the key's rows and search docs as they were
    (``untouched``), and a split-set replacement must report the key
    (``failed_manifest``).  The convergence checks then re-ingest the same
    bytes healthily.  Returns the run's event counts.
    """
    steps = generate(seed, extraction_modes=False)
    if kind == "new-live":
        first = replace(steps[0], kind="live", fail_mode=PARTIAL, whole=False, ops=("partial",))
        return run_steps(dsn, [first, *steps], run_token, seed=seed, name=name)
    partial = replace(steps[-1], kind=kind, fail_mode=PARTIAL, whole=False, ops=("partial",))
    return run_steps(dsn, [*steps, partial], run_token, seed=seed, name=name)


def run_incomplete_input(
    dsn: str, seed: int, run_token: str, *, kind: str, name: str = "blank_page"
) -> Counter[str]:
    """Incomplete input (:data:`INCOMPLETE_INPUT`, #5010) on *seed*'s key.

    *seed*'s history runs, then a step of *kind* (``reingest`` or
    ``live``) in a text-first county whose PDF has a scanned page that OCR
    fails to read.  Before #5010 the scanned page was dropped and the text
    pages split as if complete.  The step must leave the key's rows and
    search docs as they were (``untouched``), and a reingest must report
    the key (``failed_manifest``).  The convergence checks then re-ingest
    healthily.  Returns the run's event counts.
    """
    steps = generate(seed, extraction_modes=False)
    last = steps[-1]
    blank = replace(
        last,
        kind=kind,
        fail_mode=INCOMPLETE_INPUT,
        whole=False,
        profile="llm_county",
        # A live capture's scraper text: the text page's layer.
        doc=last.doc or _BLANK_PAGE_TEXT_LAYER,
        ops=("blank_page",),
    )
    return run_steps(dsn, [*steps, blank], run_token, seed=seed, name=name)


def run_wrong_shape(
    dsn: str,
    seed: int,
    run_token: str,
    *,
    kind: str,
    whole: bool = False,
    name: str = "wrong_shape",
) -> Counter[str]:
    """A malformed LLM response (:data:`WRONG_SHAPE`, #5011) on *seed*'s key.

    *seed*'s history runs, then a step of *kind* (``reingest`` or ``live``)
    in a text-first county whose model answers ``{"rulings": null}``, parsed
    by the real ``LlmExtractor``.  *whole*: the step's document is one
    chunk whose extraction would be the whole document (the case where the
    empty answer replaced the key's split before #5011).  The step must
    leave the key's rows and search docs as they were (``untouched``), and
    a reingest must report the key (``failed_manifest``).  Returns the
    run's event counts.
    """
    steps = generate(seed, extraction_modes=False)
    last = steps[-1]
    wrong = replace(
        last,
        kind=kind,
        fail_mode=WRONG_SHAPE,
        whole=whole,
        profile="llm_county",
        doc=last.doc or _BLANK_PAGE_TEXT_LAYER,
        ops=("wrong_shape",),
    )
    return run_steps(dsn, [*steps, wrong], run_token, seed=seed, name=name)


# ---------------------------------------------------------------------------
# Several keys share one court (#4969)
# ---------------------------------------------------------------------------

#: The court names a shared-court scenario's events send.  The court row's
#: ``court_name`` is ``"<court>, County of <county>"``, which every search
#: doc of the court shows: a step that sends another name renames the court
#: (``upsert_court``'s change branch) and must re-index every ruling in it,
#: on every key (#4935, ``search_fields``).  A ``db`` step sends the name
#: ``reingest_from_s3.py`` derives from the S3 key, the first one.
COURT_NAMES = ("Superior Court", "Superior Court of California", "Superior Court, Civil Division")

_SHARED_OPS = (
    "add",  # list a ruling no key lists yet
    "repost",  # list a ruling another key lists (the same text and case)
    "drop",  # stop listing one of the key's rulings
    "drop_shared",  # stop listing a ruling another key lists too
    "judge_none",  # a ruling only this key lists loses its judge
    "judge_set",  # ... or prints another roster spelling
    "court_rename",  # the step's events send another court name
    "case_miss",  # the key's LLM misses one ruling's case number
    "case_fix",  # ... and reads it again
    "same",
)


@dataclass(frozen=True)
class SharedStep:
    """One step of a shared-court scenario: *step* runs on key number
    *key*, its events naming the court *court*."""

    key: int
    step: Step
    court: str


def generate_shared(seed: int) -> tuple[int, list[SharedStep]]:
    """Return ``(number of keys, steps)`` of shared-court scenario *seed*.

    Two or three keys share one county, so one court, its cases and its
    judge roster (#4969).  The court posts a pool of rulings and each key's
    document lists some of them.  A ruling two keys list is a repost: the
    same text and case, so the later write loses content-hash dedup (#2458)
    and its document is superseded by the holder's.  Each pool ruling
    prints one judge spelling (or none) on every key that lists it.

    Every ruling has a caption, so validation rejects none, and a step's
    document text, when it has one, prints every listed number: each ruling
    a key's last extraction returned is held, active, by a key that lists
    it (``printed_somewhere``).

    Its own random stream (``"<seed>:shared"``), so :func:`generate`'s
    seeds keep their sequences.
    """
    rng = random.Random(f"{seed}:shared")
    token = f"x{seed}"
    n_keys = rng.randint(2, 3)
    judges = rng.sample(ROSTER_JUDGES, rng.choice((1, 1, 2)))
    roster = tuple(j.roster for j in judges)

    def spelling() -> str:
        judge = rng.choice(judges)
        return judge.printed if rng.random() < 0.6 else rng.choice(judge.spellings())

    # At most ten rulings: ``_case_number`` repeats one digit per index.
    pool = [
        Spec(
            i,
            _case_number(i),
            _title(i),
            _text(token, i),
            None if rng.random() < 0.15 else spelling(),
        )
        for i in range(rng.randint(5, 10))
    ]
    posted: list[list[int]] = [[] for _ in range(n_keys)]
    #: Per key, the rulings whose case number its LLM misses (the
    #: document still prints it): the ruling sits on its placeholder case
    #: until a read finds the number, maybe held by another key by then.
    missed: list[set[int]] = [set() for _ in range(n_keys)]
    steps: list[SharedStep] = []
    court = COURT_NAMES[0]

    def spec(k: int, i: int) -> Spec:
        if i in missed[k]:
            return replace(pool[i], case_number=None, printed=pool[i].case_number)
        return pool[i]

    #: Per ruling, every key that has listed it.  A pre-split capture
    #: leaves its earlier rulings on other slots, so a key may still hold
    #: a ruling it no longer lists.
    listed_by: list[set[int]] = [set() for _ in pool]

    def listers(i: int) -> int:
        return sum(i in p for p in posted)

    def emit(k: int, ops: list[str], *, first: bool = False) -> None:
        nonlocal court
        for i in posted[k]:
            listed_by[i].add(k)
        kind = rng.choices(("live", "reingest", "db", "presplit"), weights=(3, 4, 2, 2))[0]
        if first and kind == "db":
            kind = "reingest"  # DB-row mode re-ingests only keys it holds
        if kind == "db":
            court = COURT_NAMES[0]
        elif "court_rename" in ops:
            court = rng.choice([c for c in COURT_NAMES if c != court])
        specs = tuple(spec(k, i) for i in posted[k])
        rows = tuple(Row("ruling", n) for n in range(len(specs)))
        mode = rng.choices(("text", "none"), weights=(4, 1))[0]
        step = Step(
            kind,
            rows,
            specs,
            tuple(ops),
            doc=document_text(rows, specs, mode),
            roster=roster if not steps else (),
            party_tag=token,
        )
        steps.append(SharedStep(k, step, court))

    for k in range(n_keys):
        free = [i for i in range(len(pool)) if not listers(i)]
        rng.shuffle(free)
        posted[k] = sorted(free[: rng.randint(1, 3)])
        ops = ["add"]
        others = [i for p in posted[:k] for i in p]
        if others and (not posted[k] or rng.random() < 0.4):
            posted[k].insert(rng.randint(0, len(posted[k])), rng.choice(others))
            ops.append("repost")
        emit(k, ops, first=True)

    for _ in range(rng.randint(3, 8)):
        k = rng.randrange(n_keys)
        ops = [rng.choice(_SHARED_OPS) for _ in range(rng.randint(1, 2))]
        mine = posted[k]
        for op in ops:
            if op == "add":
                free = [i for i in range(len(pool)) if not listers(i)]
                if free:
                    mine.insert(rng.randint(0, len(mine)), rng.choice(free))
            elif op == "repost":
                others = sorted(
                    {i for j, p in enumerate(posted) if j != k for i in p if i not in mine}
                )
                if others:
                    mine.insert(rng.randint(0, len(mine)), rng.choice(others))
            elif op == "drop" and len(mine) > 1:
                mine.pop(rng.randrange(len(mine)))
            elif op == "drop_shared" and len(mine) > 1:
                shared = [n for n, i in enumerate(mine) if listers(i) > 1]
                if shared:
                    mine.pop(rng.choice(shared))
            elif op in ("judge_none", "judge_set"):
                # Only a ruling no other key has listed: the harness keeps
                # one printed spelling per ruling text (``KeyHarness._spelling``).
                own = [i for i in mine if listed_by[i] <= {k}]
                if own:
                    i = rng.choice(own)
                    pool[i] = replace(pool[i], judge=None if op == "judge_none" else spelling())
            elif op == "case_miss":
                missed[k].add(rng.choice(mine))
            elif op == "case_fix" and missed[k]:
                missed[k].discard(rng.choice(sorted(missed[k])))
        emit(k, ops)
    return n_keys, steps


def run_shared_court(
    dsn: str,
    seed: int,
    run_token: str,
    *,
    name: str = "shared",
    invariants: frozenset[str] | None = None,
) -> Counter[str]:
    """Run shared-court scenario *seed* (:func:`generate_shared`); raise
    :class:`InvariantError` on the first broken invariant.

    Every step holds the per-step invariants, and the court-wide ones run
    across all the keys: ``search_fields`` (a court rename, a case retitle
    or a judge rename made by one key's write re-indexes the other keys'
    rulings), ``judge_converge`` (each document votes as it stands; one
    roster judge is one judge row in the court) and ``printed_somewhere``.
    The keys are tagged with *run_token* and *name*, so concurrent runs
    never share a court.  Returns the ``telemetry_event`` counts the run
    logged.
    """
    n_keys, steps = generate_shared(seed)
    tag = f"{run_token}-{seed}-{name}"
    harness = KeyHarness(dsn, run_token, seed, invariants)
    try:
        keys = [make_key(f"{tag}-k{k}", county_tag=tag) for k in range(n_keys)]
        printed: dict[str, set[str]] = {}
        for n, shared in enumerate(steps):
            key = keys[shared.key]
            step = shared.step
            # DB-row mode selects only keys with an active document.
            selected = step.kind != "db" or any(
                r.status == "active" for r in read_key(harness.checker, key.s3_key)
            )
            harness.event_extra = {"court": shared.court}
            try:
                harness.run_step(
                    key, step, f"step {n + 1}/{len(steps)} k{shared.key} court={shared.court!r}"
                )
            finally:
                harness.event_extra = {}
            texts = {step.specs[r.spec].text for r in step.rows}
            if step.kind == "presplit":
                # A pre-split capture replaces only its own children's
                # slots: it runs no stale-child cleanup, so the key may
                # keep rulings its earlier captures printed on other slots
                # (``_SplitBatch.pre_split``).
                kept = {
                    r.text
                    for r in read_key(harness.checker, key.s3_key)
                    if r.ruling_id and r.status == "active"
                }
                printed[key.s3_key] = texts | (printed.get(key.s3_key, set()) & kept)
            elif selected:
                printed[key.s3_key] = texts
            harness._guard("printed_somewhere", harness._check_printed_somewhere, keys, printed)
            harness._guard("held_printed", harness._check_held_printed, keys, printed)
            harness._guard("judge_converge", harness._check_one_court_judge, keys)
        return harness.events
    finally:
        harness.close()
