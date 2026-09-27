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
- ``date_source``: a ruling with a ``hearing_date`` has a
  ``hearing_date_source``;
- ``synthetic_title``: a ruling on its own synthetic ``UNKNOWN-<document id>``
  case carries that ruling's own caption, never a previous slot occupant's;
- ``converge``: re-ingesting the final bytes leaves the same rows as a fresh
  ingest of those bytes, and a fresh live capture and a fresh reingest of the
  same bytes leave the same rows.

A *ruling* is identified by its text and case number (a synthetic
``UNKNOWN-*`` case counts as one case number, ``UNKNOWN``), so a ruling that
moves between split slots, or is relinked to a better case, is still tracked.

The extractor is stubbed: each step decides the rows the multimodal
extractor returns for the key's PDF.  Everything downstream of it (the
worker's split dispatch, validation, the case/document/ruling writes, the
sibling move, content-hash dedup, stale-child cleanup, search indexing) is
the production code.

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
import random
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg

from framework.llm_schema import ExtractedRuling
from ingestion.split_ids import derive_parent_document_id, make_split_document_id

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
    "date_source",
    "synthetic_title",
    "converge",
    "error",
)


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
                if spec.title is None:
                    tag += "~"
                parts.append(tag)
        return f"{self.kind}[{' '.join(parts)}] ops={list(self.ops)}"


def _case_number(idx: int) -> str:
    # Five repeated digits: every pair is Levenshtein 5 apart, beyond the
    # enrichment fuzzy matcher's reach (distance 2).
    return f"30-2026-{str(idx % 10) * 5}"


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


def generate(seed: int) -> list[Step]:
    """Return the step sequence for *seed* (deterministic)."""
    rng = random.Random(seed)
    token = f"s{seed}"
    n_specs = rng.randint(3, 7)
    specs = [
        Spec(
            idx=i,
            case_number=None if rng.random() < 0.2 else _case_number(i),
            title=None if rng.random() < 0.3 else _title(i),
            text=_text(token, i),
        )
        for i in range(n_specs)
    ]
    order = list(range(n_specs))
    rng.shuffle(order)
    first = sorted(order[: rng.randint(1, n_specs)])
    rows = [Row("ruling", i) for i in first]
    steps = [Step(rng.choice(("live", "reingest", "db")), tuple(rows), tuple(specs))]

    for _ in range(rng.randint(2, 6)):
        ops = [rng.choice(_OPS) for _ in range(rng.randint(1, 2))]
        for op in ops:
            rows, specs = _apply_op(rng, op, rows, specs)
        kind = rng.choices(("live", "reingest", "db"), weights=(3, 4, 2))[0]
        steps.append(Step(kind, tuple(rows), tuple(specs), tuple(ops)))
    return steps


def _apply_op(
    rng: random.Random, op: str, rows: list[Row], specs: list[Spec]
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
            spec = replace(spec, case_number=_case_number(spec.idx))
        specs[i] = spec
    return rows, specs


def extracted_rulings(step: Step) -> list[ExtractedRuling]:
    """The multimodal extractor's output for *step*."""
    out: list[ExtractedRuling] = []
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
    """Records the search index as ``{document id: indexed ruling text}``."""

    def __init__(self) -> None:
        self.docs: dict[str, str | None] = {}

    def index_document(self, event: dict[str, Any], *, force: bool = False) -> bool:
        self.docs[str(event["document_id"])] = event.get("ruling_text")
        return True

    def delete_documents(self, document_ids: list[str]) -> int:
        for doc_id in document_ids:
            self.docs.pop(str(doc_id), None)
        return len(document_ids)


class FakeExtractor:
    """Multimodal extractor double: returns the current step's rows."""

    def __init__(self) -> None:
        self.rulings: list[ExtractedRuling] = []

    def extract_from_pdf(self, _pdf: bytes, **_kwargs: Any) -> list[ExtractedRuling]:
        return [r.model_copy() for r in self.rulings]


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


def make_key(tag: str) -> Key:
    content_hash = hashlib.sha256(tag.encode()).hexdigest()
    parent = derive_parent_document_id(content_hash)
    # Each key gets its own county (so its own court and case rows); the
    # slug is in the key, where the reingest script reads the county from.
    county_slug = f"harness_{hashlib.sha1(tag.encode()).hexdigest()[:10]}"
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
        self._pending: InvariantError | None = None
        self.conn = _CheckedConn(self.real, self._after_commit)
        #: alert id -> (s3_key, ruling identity)
        self._alerts: dict[str, tuple[str, tuple[str, str]]] = {}
        self._user_sub: str | None = None
        self.worker = self._make_worker()

    def close(self) -> None:
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
        worker._validation_enabled = False
        worker._formatting_enabled = False
        worker._summarization_enabled = False
        return worker

    def _event(self, key: Key, kind: str) -> dict[str, Any]:
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
            "ruling_text": "",
        }
        if kind == "live":
            event["scraper_id"] = "ca-harness-tentatives"
            event["hearing_date"] = HEARING.isoformat()
        else:
            event["scraper_id"] = "reingest-ca-harness"
            event["_replace_split_set"] = True
        return event

    # -- running --------------------------------------------------------

    def run_step(self, key: Key, step: Step, label: str) -> None:
        """Run one step on *key*; raise :class:`InvariantError` on a
        broken invariant."""
        self._key = key
        self._step_label = f"seed={self.seed} {label} {step.describe()}"
        self._attach_alerts(key)
        before = ruling_identities(read_key(self.checker, key.s3_key))
        self._snapshots = []
        self._pending = None
        self.extractor.rulings = extracted_rulings(step)
        try:
            if step.kind == "db":
                DB_MODE_DRIVER(self, key, step)
            else:
                self.worker.process_event(self._event(key, step.kind))
        except InvariantError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a violation
            self._fail("error", f"step raised {type(exc).__name__}: {exc}")
        finally:
            try:
                self.real.rollback()
            except Exception:  # noqa: BLE001
                pass
        if self._pending is not None:
            raise self._pending
        after_rows = read_key(self.checker, key.s3_key)
        after = ruling_identities(after_rows)
        self._guard("gap", self._check_gap, before, after)
        self._guard("alert", self._check_alerts, key, after_rows)
        self._guard("search", self._check_search, key, after_rows)
        if step.kind != "db":
            # A DB-row reingest only rewrites rows it selects, so it can
            # leave a slot untouched; the worker writes every ruling row.
            self._guard("synthetic_title", self._check_rows, key, after_rows, step)

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
        try:
            self._guard("superseded", self._check_supersede, key, rows)
            self._guard("date_source", self._check_date_source, key, rows)
        except InvariantError as exc:
            self._pending = exc

    def _check_date_source(self, key: Key, rows: list[DocRow]) -> None:
        for r in rows:
            if r.ruling_id and r.hearing_date is not None and r.hearing_date_source is None:
                self._fail(
                    "date_source",
                    f"ruling on {key.label(r.id)} has hearing_date {r.hearing_date} "
                    "and no hearing_date_source",
                )

    # -- invariants -----------------------------------------------------

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
        present = ruling_identities(rows)
        with self.checker.cursor() as cur:
            for alert_id, (s3_key, identity) in self._alerts.items():
                if s3_key != key.s3_key or not present[identity]:
                    continue
                cur.execute(
                    "SELECT r.ruling_text, c.case_number FROM alert_events a "
                    "LEFT JOIN rulings r ON r.id = a.ruling_id "
                    "LEFT JOIN cases c ON c.id = r.case_id WHERE a.id = %s::uuid",
                    (alert_id,),
                )
                text, case_number = cur.fetchone()
                if (text, _norm_case(case_number)) != identity:
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
) -> None:
    """Run *seed*'s sequence and the convergence checks; raise
    :class:`InvariantError` on the first broken invariant.  Two runs of one
    seed in one session need distinct *name*s (their keys derive from it).
    *invariants* limits the check to those invariants (default: all)."""
    run_steps(dsn, generate(seed), run_token, seed=seed, name=name, invariants=invariants)


def run_steps(
    dsn: str,
    steps: list[Step],
    run_token: str,
    *,
    seed: int = -1,
    name: str = "seq",
    setup: Callable[[KeyHarness, Key], None] | None = None,
    invariants: frozenset[str] | None = None,
) -> None:
    """Run *steps* on one key, then the convergence checks.  *setup*, when
    given, prepares the key's starting state; *invariants* limits the check
    to those invariants (default: all)."""
    harness = KeyHarness(dsn, run_token, seed, invariants)
    try:
        key = make_key(f"{run_token}-{seed}-{name}-main")
        if setup is not None:
            setup(harness, key)
        for n, step in enumerate(steps):
            harness.run_step(key, step, f"step {n + 1}/{len(steps)}")

        final = steps[-1]
        if final.kind == "live":
            # A live write keeps an established case link (preserve-first,
            # #2475), so its result may depend on history by design.  A
            # split-set replacement must not: re-ingest the same bytes.
            settle = Step("reingest", final.rows, final.specs, ("settle",))
            harness.run_step(key, settle, "settle")
        history = normalized(key, read_key(harness.checker, key.s3_key))

        fresh = {}
        for kind in ("reingest", "live"):
            fresh_key = make_key(f"{run_token}-{seed}-{name}-fresh-{kind}")
            harness.run_step(fresh_key, replace(final, kind=kind), f"fresh {kind}")
            fresh[kind] = normalized(fresh_key, read_key(harness.checker, fresh_key.s3_key))

        if "converge" not in harness.invariants:
            return
        if not _converged(history, fresh["reingest"]):
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
    finally:
        harness.close()


def _converged(
    history: tuple[frozenset[tuple], frozenset[str]],
    fresh: tuple[frozenset[tuple], frozenset[str]],
) -> bool:
    """Whether a key re-ingested after a history holds the rows a fresh
    ingest of the same bytes holds.

    One difference is allowed: where the fresh ingest found no case number
    (``UNKNOWN``), the key may keep the real case its ruling had before.  A
    re-extraction that misses a case number must not throw away the one an
    earlier extraction of the same bytes found (#4788).
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
        if not (case == "UNKNOWN" and h_case != "UNKNOWN"):
            return False
    return True


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
