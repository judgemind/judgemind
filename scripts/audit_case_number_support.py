#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit: is each active ruling's case number supported by its raw document?

For every active ruling with a real case number (not ``UNKNOWN-``) in the
given counties, fetch the document's raw S3 object, extract its text the way
the ingestion worker does (``llm_extract.extract_text_from_pdf``), and ask the
worker's own case-number gate (``ingestion.case_evidence``) whether that text
refutes the number.  The audit uses the gate's predicate, not a copy of it, so
it cannot disagree with ingest (#4924, #4927).

Verdicts, counted per ruling:

- ``supported``    the document text carries the case number.
- ``unsupported``  the text can settle the number and does not carry it: an
                   invented or fuzzy-corrected number (#4907, #4915).
- ``no_text``      the text is too short to be evidence
                   (``case_evidence.document_evidence`` is empty).
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
file listing S3 keys to audit, one per line), ``--limit N`` (at most N keys
per county), ``--timeout SECS`` (per key, default 180), ``--workers N``
(default 8), ``--progress-every N`` (default 100).
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


def audit_key(key: str, rulings: list[Ruling], gate: Gate) -> KeyResult:
    try:
        text = text_for(gate.fetch(key), key, gate.extract_pdf, gate.is_pdf)
    except Exception as exc:  # noqa: BLE001 — every failure is reported per key
        return KeyResult(
            key, [("error", r) for r in rulings], f"{type(exc).__name__}: {exc}"
        )
    return KeyResult(key, judge_rulings(rulings, text, gate.evidence_for, gate.refuted))


def production_gate() -> Gate:
    """The worker's extractor and the ingest gate's predicate."""
    import boto3
    from ingestion.case_evidence import case_number_refuted, document_evidence
    from ingestion.llm_extract import extract_text_from_pdf, is_pdf_binary

    clients: dict[int, Any] = {}

    def fetch(key: str) -> bytes:
        # One client per process: a forked child must not reuse its parent's.
        client = clients.get(os.getpid())
        if client is None:
            client = clients[os.getpid()] = boto3.client("s3")
        return client.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    return Gate(
        fetch=fetch,
        extract_pdf=extract_text_from_pdf,
        is_pdf=is_pdf_binary,
        evidence_for=lambda text: document_evidence(text),
        refuted=case_number_refuted,
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
        "--keys", help="s3://bucket/key or local file listing the S3 keys to audit."
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
    args = parser.parse_args(argv)
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
    rows = fetch_county_rows(os.environ["DATABASE_URL"], args.all_counties)
    return run(args, rows, production_gate(), only, out=_print)


if __name__ == "__main__":
    sys.exit(main())
