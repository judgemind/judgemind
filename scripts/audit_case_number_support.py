#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit: is each active ruling's case number supported by its raw document?

For every active ruling with a real case number (not ``UNKNOWN-``) in the
given counties, fetch the document's raw S3 object, extract the text the
ingestion worker's gate counts as evidence (a PDF's text layer,
``llm_extract.extract_text_layer``), and ask the worker's own case-number
gate (``ingestion.case_evidence``) whether that text refutes the number.  The
audit uses the gate's predicate, not a copy of it, so it cannot disagree with
ingest (#4924, #4927).  A PDF with no text layer is OCR'd by the worker, but
OCR text is never evidence (#4986), so the audit skips the OCR: such a PDF's
rulings are ``no_text``, exactly as the gate judges them.

Verdicts, counted per ruling:

- ``supported``    the document text carries the case number.
- ``unsupported``  the text can settle the number and does not carry it: an
                   invented or fuzzy-corrected number (#4907, #4915).
- ``no_text``      the text is too short to be evidence, or there is no
                   text layer (``case_evidence.document_evidence`` is empty).
- ``timeout``      extracting the key's text took longer than ``--timeout``.
                   Each key runs in its own forked process, which is killed
                   at the deadline: pdfplumber cannot be interrupted in a
                   thread, and one stalled PDF used to hang a whole county.
- ``error``        fetching or extracting the key raised.

Output: ``PROGRESS`` every ``--progress-every`` keys, one ``UNSUPPORTED`` /
``TIMEOUT`` / ``ERROR`` line per finding, and ``SUMMARY <county>: {json}``
per county.  Exit 1 when any ruling is unsupported, so the audit can gate a
verification; timeouts and errors are reported but do not fail it.

Usage (dev DB is private, so run it on ECS)::

    scripts/ecs-run-task.sh --detach scripts/audit_case_number_support.py \\
        -- --county Orange --county Riverside
    scripts/ecs-wait-task.sh

Options: positional counties or ``--county NAME`` (repeatable; default
Orange and Riverside), ``--keys PATH`` (an ``s3://bucket/key`` URI or local
file listing S3 keys to audit, one per line; for ECS runs upload it to
``s3://judgemind-document-archive-dev/tmp/keys/<name>.txt``, #4803), ``--limit N`` (at most N keys
per county), ``--timeout SECS`` (per key, default 180), ``--workers N``
(default 8), ``--progress-every N`` (default 100).

No-text PDF mode (``--no-text-pdfs``, #4983): instead of judging rulings,
list every archived PDF under ``ca/<county>/`` in S3 and report the ones
whose text layer is too short to be evidence (the same ``document_evidence``
rule: a scanned PDF with no text layer, which the worker OCRs), with each
key's page count and how many active rulings it holds today (#4986).
Output: one ``NO_TEXT`` line per such key (``chars``, ``pages``,
``rulings``) and ``SUMMARY_NO_TEXT <county>: {json}`` per county
(``pdf_keys``, ``no_text``, ``no_text_zero_rulings``, ``no_text_rulings``,
``no_text_over_10_pages``, ``timeout``, ``error``).  ``--min-pages N``
reports only no-text keys of at least N pages.  ``--write-keys
s3://bucket/key`` also writes the reported keys, one per line, for a
reingest's ``--s3-key-list``.  This mode always exits 0.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import multiprocessing.connection
import os
import sys
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

# The scraper-framework source: repo checkout locally, /app in the ECS image.
for _path in (
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "packages",
        "scraper-framework",
        "src",
    ),
    "/app/packages/scraper-framework/src",
    "/app/src",
    "/app",
):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

BUCKET = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev")
DEFAULT_COUNTIES = ("Orange", "Riverside")
VERDICTS = ("supported", "unsupported", "no_text", "timeout", "error")

SQL = """
SELECT d.s3_key, d.id::text, r.id::text, c.case_number
FROM derived.rulings r
JOIN derived.documents d ON d.id = r.document_id
JOIN derived.cases c ON c.id = r.case_id
JOIN derived.courts ct ON ct.id = r.court_id
WHERE ct.county = %s AND d.status = 'active' AND d.s3_key IS NOT NULL
  AND c.case_number IS NOT NULL AND c.case_number NOT LIKE 'UNKNOWN-%%'
ORDER BY d.s3_key, r.id
"""


@dataclass(frozen=True)
class Ruling:
    s3_key: str
    document_id: str
    ruling_id: str
    case_number: str


@dataclass
class KeyResult:
    """What auditing one S3 key produced."""

    key: str
    verdicts: list[tuple[str, Ruling]]
    detail: str | None = None  # error class / timeout note


@dataclass
class CountyReport:
    county: str
    keys: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    unsupported: list[Ruling] = field(default_factory=list)
    timeout_keys: list[str] = field(default_factory=list)
    error_keys: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            **{v: self.counts.get(v, 0) for v in VERDICTS},
            "keys": self.keys,
            "rulings": sum(self.counts.values()),
            "timeout_keys": self.timeout_keys,
            "error_keys": self.error_keys,
            "unsupported_rows": [
                {
                    "document_id": r.document_id,
                    "ruling_id": r.ruling_id,
                    "case_number": r.case_number,
                    "s3_key": r.s3_key,
                }
                for r in self.unsupported
            ],
            "elapsed_s": round(self.elapsed_s, 1),
        }


# ---------------------------------------------------------------------------
# Verdict logic (runs inside the per-key child process)
# ---------------------------------------------------------------------------


def text_for(
    body: bytes,
    key: str,
    extract_pdf: Callable[[Any], str | None],
    is_pdf: Callable[[Any], bool],
) -> str | None:
    """The document text the worker would see for a raw S3 object."""
    if key.endswith(".pdf") or is_pdf(body):
        return extract_pdf(body)
    text = body.decode("utf-8", errors="replace")
    if is_pdf(text):
        return extract_pdf(text)
    return text


def judge_rulings(
    rulings: Iterable[Ruling],
    text: str | None,
    evidence_for: Callable[[str | None], tuple[str, ...]],
    refuted: Callable[[str, tuple[str, ...]], bool],
) -> list[tuple[str, Ruling]]:
    """Verdict per ruling, from the gate's ``document_evidence`` and
    ``case_number_refuted``."""
    evidence = evidence_for(text)
    if not evidence:
        return [("no_text", r) for r in rulings]
    return [
        ("unsupported" if refuted(r.case_number, evidence) else "supported", r)
        for r in rulings
    ]


@dataclass(frozen=True)
class Gate:
    """The production functions the audit calls; injectable for tests."""

    fetch: Callable[[str], bytes]
    extract_pdf: Callable[[Any], str | None]
    is_pdf: Callable[[Any], bool]
    evidence_for: Callable[[str | None], tuple[str, ...]]
    refuted: Callable[[str, tuple[str, ...]], bool]
    page_count: Callable[[Any], int] = lambda content: 0


def audit_key(key: str, rulings: list[Ruling], gate: Gate) -> KeyResult:
    try:
        text = text_for(gate.fetch(key), key, gate.extract_pdf, gate.is_pdf)
    except Exception as exc:  # noqa: BLE001 — every failure is reported per key
        return KeyResult(
            key, [("error", r) for r in rulings], f"{type(exc).__name__}: {exc}"
        )
    return KeyResult(key, judge_rulings(rulings, text, gate.evidence_for, gate.refuted))


def production_gate() -> Gate:
    """The worker's evidence text (a PDF's text layer: OCR text is never
    evidence, #4986) and the ingest gate's predicate."""
    import boto3
    from ingestion.case_evidence import case_number_refuted, document_evidence
    from ingestion.llm_extract import extract_text_layer, is_pdf_binary, pdf_page_count

    clients: dict[int, Any] = {}

    def fetch(key: str) -> bytes:
        # One client per process: a forked child must not reuse its parent's.
        client = clients.get(os.getpid())
        if client is None:
            client = clients[os.getpid()] = boto3.client("s3")
        return client.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    return Gate(
        fetch=fetch,
        extract_pdf=extract_text_layer,
        is_pdf=is_pdf_binary,
        evidence_for=lambda text: document_evidence(text),
        refuted=case_number_refuted,
        page_count=pdf_page_count,
    )


# ---------------------------------------------------------------------------
# Per-key process pool with a hard deadline
# ---------------------------------------------------------------------------


def _child(
    conn: multiprocessing.connection.Connection, fn: Callable[..., Any], args: tuple
) -> None:
    try:
        conn.send(("ok", fn(*args)))
    except BaseException as exc:  # noqa: BLE001 — report, never hang the parent
        conn.send(("raised", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def run_with_deadline(
    jobs: Iterable[tuple[str, tuple]],
    fn: Callable[..., Any],
    *,
    workers: int,
    timeout: float,
) -> Iterator[tuple[str, str, Any]]:
    """Run ``fn(*args)`` for each ``(name, args)`` job in its own forked
    process, at most *workers* at a time.

    Yields ``(name, "ok", result)``, ``(name, "timeout", None)`` when the job
    outlived *timeout* seconds (its process is killed), or
    ``(name, "error", detail)`` when it raised or its process died.
    """
    ctx = multiprocessing.get_context("fork")
    pending = deque(jobs)
    running: dict[Any, tuple[str, Any, float]] = {}  # conn -> (name, proc, deadline)
    while pending or running:
        while pending and len(running) < workers:
            name, args = pending.popleft()
            parent_conn, child_conn = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_child, args=(child_conn, fn, args), daemon=True)
            proc.start()
            child_conn.close()
            running[parent_conn] = (name, proc, time.monotonic() + timeout)

        wait_for = max(0.0, min(d for _, _, d in running.values()) - time.monotonic())
        ready = set(multiprocessing.connection.wait(list(running), timeout=wait_for))
        now = time.monotonic()
        for conn in list(running):
            name, proc, deadline = running[conn]
            if conn in ready:
                del running[conn]
                try:
                    status, payload = conn.recv()
                except EOFError:
                    status, payload = (
                        "raised",
                        f"worker exited with code {proc.exitcode}",
                    )
                conn.close()
                proc.join(timeout=5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
                yield (
                    (name, "ok", payload)
                    if status == "ok"
                    else (name, "error", payload)
                )
            elif now >= deadline:
                del running[conn]
                proc.kill()
                proc.join()
                conn.close()
                yield name, "timeout", None


# ---------------------------------------------------------------------------
# County audit
# ---------------------------------------------------------------------------


def group_by_key(
    rows: Iterable[Ruling], only: set[str] | None, limit: int | None
) -> dict[str, list[Ruling]]:
    by_key: dict[str, list[Ruling]] = defaultdict(list)
    for row in rows:
        if only is None or row.s3_key in only:
            by_key[row.s3_key].append(row)
    keys = sorted(by_key)
    if limit is not None:
        keys = keys[:limit]
    return {k: by_key[k] for k in keys}


def audit_county(
    county: str,
    by_key: dict[str, list[Ruling]],
    gate: Gate,
    *,
    workers: int,
    timeout: float,
    progress_every: int,
    out: Callable[[str], None] = print,
) -> CountyReport:
    report = CountyReport(county=county, keys=len(by_key))
    start = time.monotonic()
    jobs = ((key, (key, rulings, gate)) for key, rulings in by_key.items())
    results = run_with_deadline(jobs, audit_key, workers=workers, timeout=timeout)
    for done, (key, status, payload) in enumerate(results, start=1):
        rulings = by_key[key]
        if status == "timeout":
            report.counts["timeout"] += len(rulings)
            report.timeout_keys.append(key)
            out(f"TIMEOUT {county} key={key} rulings={len(rulings)} after={timeout:g}s")
        elif status == "error" or payload.detail is not None:
            detail = payload if status == "error" else payload.detail
            report.counts["error"] += len(rulings)
            report.error_keys.append(key)
            out(f"ERROR {county} key={key} rulings={len(rulings)} error={detail}")
        else:
            for verdict, r in payload.verdicts:
                report.counts[verdict] += 1
                if verdict == "unsupported":
                    report.unsupported.append(r)
                    out(
                        f"UNSUPPORTED {county} doc={r.document_id} ruling={r.ruling_id} "
                        f"case={r.case_number} key={r.s3_key}"
                    )
        if progress_every and done % progress_every == 0 and done < report.keys:
            out(
                f"PROGRESS {county}: {done}/{report.keys} keys "
                f"elapsed={time.monotonic() - start:.0f}s {json.dumps(dict(report.counts), sort_keys=True)}"
            )
    report.elapsed_s = time.monotonic() - start
    out(f"SUMMARY {county}: {json.dumps(report.summary(), sort_keys=True)}")
    return report


# ---------------------------------------------------------------------------
# No-text PDF mode (#4983)
# ---------------------------------------------------------------------------

NO_TEXT_RULING_COUNTS_SQL = """
SELECT d.s3_key, count(r.id)
FROM derived.documents d
LEFT JOIN derived.rulings r ON r.document_id = d.id
WHERE d.status = 'active' AND d.s3_key LIKE %s
GROUP BY d.s3_key
"""


def county_prefix(county: str, state: str = "ca") -> str:
    """The archive prefix of a county's raw objects: ``ca/santa_clara/``."""
    return f"{state.lower()}/{county.strip().lower().replace(' ', '_')}/"


def is_raw_pdf_key(key: str) -> bool:
    """A raw PDF capture: content-addressed (``<state>/<county>/<court>/raw/
    <sha>.pdf``) or dated (``.../raw/YYYY/MM/DD/<uuid>.pdf``)."""
    parts = key.split("/")
    return len(parts) >= 5 and parts[3] == "raw" and key.endswith(".pdf")


@dataclass(frozen=True)
class TextProbe:
    """What one key's text extraction produced."""

    key: str
    chars: int  # non-blank characters of the text layer (stripped length)
    evidence: bool  # True when the gate counts the text as evidence
    pages: int = 0  # page count, read only for a key with no evidence


#: The page count past which the OCR fallback used to stop reading (#4986).
OLD_OCR_PAGE_CAP = 10


def probe_key_text(key: str, gate: Gate) -> TextProbe:
    body = gate.fetch(key)
    text = text_for(body, key, gate.extract_pdf, gate.is_pdf)
    usable = text if text and not gate.is_pdf(text) else ""
    evidence = bool(gate.evidence_for(text))
    pages = 0 if evidence else gate.page_count(body)
    return TextProbe(key, len(usable.strip()), evidence, pages)


@dataclass
class NoTextReport:
    county: str
    pdf_keys: int = 0
    no_text: list[tuple[str, int, int, int]] = field(
        default_factory=list
    )  # key, chars, pages, rulings
    timeout_keys: list[str] = field(default_factory=list)
    error_keys: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "pdf_keys": self.pdf_keys,
            "no_text": len(self.no_text),
            "no_text_zero_rulings": sum(1 for *_, n in self.no_text if n == 0),
            "no_text_rulings": sum(n for *_, n in self.no_text),
            "no_text_over_10_pages": sum(
                1 for _, _, p, _ in self.no_text if p > OLD_OCR_PAGE_CAP
            ),
            "timeout": len(self.timeout_keys),
            "error": len(self.error_keys),
            "timeout_keys": self.timeout_keys,
            "error_keys": self.error_keys,
        }


def audit_no_text_county(
    county: str,
    keys: list[str],
    ruling_counts: dict[str, int],
    gate: Gate,
    *,
    workers: int,
    timeout: float,
    progress_every: int,
    min_pages: int = 0,
    out: Callable[[str], None] = print,
) -> NoTextReport:
    report = NoTextReport(county=county, pdf_keys=len(keys))
    jobs = ((key, (key, gate)) for key in keys)
    results = run_with_deadline(jobs, probe_key_text, workers=workers, timeout=timeout)
    for done, (key, status, payload) in enumerate(results, start=1):
        if status == "timeout":
            report.timeout_keys.append(key)
            out(f"TIMEOUT {county} key={key} after={timeout:g}s")
        elif status == "error":
            report.error_keys.append(key)
            out(f"ERROR {county} key={key} error={payload}")
        elif not payload.evidence and payload.pages >= min_pages:
            rulings = ruling_counts.get(key, 0)
            report.no_text.append((key, payload.chars, payload.pages, rulings))
            out(
                f"NO_TEXT {county} key={key} chars={payload.chars} "
                f"pages={payload.pages} rulings={rulings}"
            )
        if progress_every and done % progress_every == 0 and done < report.pdf_keys:
            out(
                f"PROGRESS {county}: {done}/{report.pdf_keys} keys no_text={len(report.no_text)}"
            )
    out(f"SUMMARY_NO_TEXT {county}: {json.dumps(report.summary(), sort_keys=True)}")
    return report


def run_no_text(
    args: argparse.Namespace,
    keys_by_county: dict[str, list[str]],
    ruling_counts: dict[str, int],
    gate: Gate,
    only: set[str] | None,
    out: Callable[[str], None] = print,
) -> list[NoTextReport]:
    reports = []
    for county in args.all_counties:
        keys = sorted(
            k for k in keys_by_county.get(county, []) if only is None or k in only
        )
        if args.limit is not None:
            keys = keys[: args.limit]
        out(f"START_NO_TEXT {county}: {len(keys)} pdf keys")
        reports.append(
            audit_no_text_county(
                county,
                keys,
                ruling_counts,
                gate,
                workers=args.workers,
                timeout=args.timeout,
                progress_every=args.progress_every,
                min_pages=args.min_pages,
                out=out,
            )
        )
    return reports


def no_text_key_list(reports: Iterable[NoTextReport]) -> str:
    return "".join(f"{key}\n" for r in reports for key, *_ in r.no_text)


def list_county_pdf_keys(
    s3: Any, bucket: str, counties: list[str]
) -> dict[str, list[str]]:
    paginator = s3.get_paginator("list_objects_v2")
    out: dict[str, list[str]] = {}
    for county in counties:
        out[county] = [
            obj["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=county_prefix(county))
            for obj in page.get("Contents", [])
            if is_raw_pdf_key(obj["Key"])
        ]
    return out


def fetch_ruling_counts(database_url: str, counties: list[str]) -> dict[str, int]:
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(database_url) as conn:
        for county in counties:
            with conn.cursor() as cur:
                cur.execute(NO_TEXT_RULING_COUNTS_SQL, (county_prefix(county) + "%",))
                counts.update({key: int(n) for key, n in cur.fetchall()})
    return counts


def main_no_text(args: argparse.Namespace, only: set[str] | None) -> int:
    import boto3

    s3 = boto3.client("s3")
    keys = list_county_pdf_keys(s3, BUCKET, args.all_counties)
    counts = fetch_ruling_counts(os.environ["DATABASE_URL"], args.all_counties)
    reports = run_no_text(args, keys, counts, production_gate(), only, out=_print)
    if args.write_keys:
        bucket, _, key = args.write_keys.removeprefix("s3://").partition("/")
        s3.put_object(
            Bucket=bucket, Key=key, Body=no_text_key_list(reports).encode("utf-8")
        )
        _print(
            f"WROTE {sum(len(r.no_text) for r in reports)} keys to {args.write_keys}"
        )
    return 0


# ---------------------------------------------------------------------------
# Inputs and entry point
# ---------------------------------------------------------------------------


def parse_key_list(text: str) -> set[str]:
    return {
        s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")
    }


def load_key_list(path: str, s3_get: Callable[[str, str], bytes]) -> set[str]:
    if path.startswith("s3://"):
        bucket, _, key = path.removeprefix("s3://").partition("/")
        if not bucket or not key:
            raise ValueError(f"--keys must be s3://bucket/key: {path}")
        return parse_key_list(s3_get(bucket, key).decode("utf-8"))
    with open(path, encoding="utf-8") as fh:
        return parse_key_list(fh.read())


def fetch_county_rows(
    database_url: str, counties: list[str]
) -> dict[str, list[Ruling]]:
    import psycopg

    rows: dict[str, list[Ruling]] = {}
    with psycopg.connect(database_url) as conn:
        for county in counties:
            with conn.cursor() as cur:
                cur.execute(SQL, (county,))
                rows[county] = [Ruling(*row) for row in cur.fetchall()]
    return rows


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "counties", nargs="*", help="Counties to audit (same as --county)."
    )
    parser.add_argument(
        "--county", action="append", default=[], help="County to audit (repeatable)."
    )
    parser.add_argument(
        "--keys",
        help=(
            "s3://bucket/key or local file listing the S3 keys to audit.  For "
            "ECS runs use s3://judgemind-document-archive-dev/tmp/keys/<name>.txt "
            "(#4803)."
        ),
    )
    parser.add_argument("--limit", type=int, help="Audit at most N keys per county.")
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="Per-key seconds (default 180)."
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="Parallel key processes (default 8)."
    )
    parser.add_argument(
        "--progress-every", type=int, default=100, help="Progress line every N keys."
    )
    parser.add_argument(
        "--no-text-pdfs",
        action="store_true",
        help="Report archived PDFs with no usable text layer instead (#4983).",
    )
    parser.add_argument(
        "--write-keys",
        help="With --no-text-pdfs: s3://bucket/key to write the no-text keys to.",
    )
    parser.add_argument(
        "--min-pages",
        type=int,
        default=0,
        help="With --no-text-pdfs: report only no-text keys of at least N pages.",
    )
    args = parser.parse_args(argv)
    if args.write_keys and (
        not args.no_text_pdfs or not args.write_keys.startswith("s3://")
    ):
        parser.error("--write-keys needs --no-text-pdfs and an s3:// URI")
    args.all_counties = list(dict.fromkeys([*args.counties, *args.county])) or list(
        DEFAULT_COUNTIES
    )
    if (
        args.workers < 1
        or args.timeout <= 0
        or (args.limit is not None and args.limit < 1)
    ):
        parser.error("--workers, --timeout and --limit must be positive")
    return args


def run(
    args: argparse.Namespace,
    rows_by_county: dict[str, list[Ruling]],
    gate: Gate,
    only: set[str] | None,
    out: Callable[[str], None] = print,
) -> int:
    reports = []
    for county in args.all_counties:
        by_key = group_by_key(rows_by_county.get(county, []), only, args.limit)
        out(
            f"START {county}: {len(by_key)} keys, timeout={args.timeout:g}s, workers={args.workers}"
        )
        reports.append(
            audit_county(
                county,
                by_key,
                gate,
                workers=args.workers,
                timeout=args.timeout,
                progress_every=args.progress_every,
                out=out,
            )
        )
    unsupported = sum(r.counts["unsupported"] for r in reports)
    return 1 if unsupported else 0


def _print(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    only = None
    if args.keys:
        import boto3

        s3 = boto3.client("s3")
        only = load_key_list(
            args.keys, lambda b, k: s3.get_object(Bucket=b, Key=k)["Body"].read()
        )
    if args.no_text_pdfs:
        return main_no_text(args, only)
    rows = fetch_county_rows(os.environ["DATABASE_URL"], args.all_counties)
    return run(args, rows, production_gate(), only, out=_print)


if __name__ == "__main__":
    sys.exit(main())
