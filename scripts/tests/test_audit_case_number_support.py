"""Tests for scripts/audit_case_number_support.py (#4924).

The script imports boto3 / psycopg / ingestion only inside its production
helpers, so the audit logic runs here with a stubbed S3 fetch, stubbed DB
rows and a stand-in for the ingest gate.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audit_case_number_support as audit

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the audit forks a process per key"
)

LONG = "x" * 250  # enough text for the stand-in gate to count it as evidence


def _evidence(text: str | None) -> tuple[str, ...]:
    return (text,) if text and len(text.strip()) >= 200 else ()


def _refuted(case_number: str, evidence: tuple[str, ...]) -> bool:
    return not any(case_number in t for t in evidence)


def _gate(
    objects: dict[str, bytes], extract_pdf=None, page_count=None
) -> audit.Gate:
    def fetch(key: str) -> bytes:
        if key not in objects:
            raise KeyError(key)
        return objects[key]

    return audit.Gate(
        fetch=fetch,
        extract_pdf=extract_pdf or (lambda content: "PDFTEXT " + LONG + " 30-2026-01 "),
        is_pdf=lambda content: (
            (content[:4] == b"%PDF")
            if isinstance(content, bytes)
            else content.startswith("%PDF")
        ),
        evidence_for=_evidence,
        refuted=_refuted,
        page_count=page_count or (lambda content: 0),
    )


def _r(key: str, n: int, case: str) -> audit.Ruling:
    return audit.Ruling(
        s3_key=key, document_id=f"d-{key}", ruling_id=f"r{n}", case_number=case
    )


def _args(*argv: str):
    return audit.parse_args(list(argv))


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------


def test_judge_rulings_supported_unsupported_and_no_text() -> None:
    rulings = [_r("a", 1, "CASE-1"), _r("a", 2, "CASE-9")]
    text = LONG + " CASE-1 "
    verdicts = audit.judge_rulings(rulings, text, _evidence, _refuted)
    assert [v for v, _ in verdicts] == ["supported", "unsupported"]
    assert [
        v for v, _ in audit.judge_rulings(rulings, "short", _evidence, _refuted)
    ] == ["no_text", "no_text"]
    assert [v for v, _ in audit.judge_rulings(rulings, None, _evidence, _refuted)] == [
        "no_text",
        "no_text",
    ]


def test_text_for_routes_pdf_and_plain_text() -> None:
    calls: list[object] = []

    def extract(content):
        calls.append(content)
        return "extracted"

    def is_pdf(c):
        return (c[:4] == b"%PDF") if isinstance(c, bytes) else c.startswith("%PDF")

    assert audit.text_for(b"%PDF-1.4 ...", "a.bin", extract, is_pdf) == "extracted"
    assert audit.text_for(b"anything", "b.pdf", extract, is_pdf) == "extracted"
    assert (
        audit.text_for(b"<html>ruling</html>", "c.html", extract, is_pdf)
        == "<html>ruling</html>"
    )
    assert len(calls) == 2


def test_audit_key_reports_fetch_error() -> None:
    result = audit.audit_key("missing", [_r("missing", 1, "C")], _gate({}))
    assert result.detail.startswith("KeyError")
    assert [v for v, _ in result.verdicts] == ["error"]


def test_production_gate_uses_the_ingest_gate() -> None:
    """The audit must call ``ingestion.case_evidence``, not a copy of it."""
    pytest.importorskip("ingestion.case_evidence")
    pytest.importorskip("boto3")
    from ingestion import case_evidence, llm_extract

    gate = audit.production_gate()
    assert gate.refuted is case_evidence.case_number_refuted
    # OCR text is never evidence (#4986): the audit reads the text layer.
    assert gate.extract_pdf is llm_extract.extract_text_layer
    assert gate.page_count is llm_extract.pdf_page_count
    assert gate.evidence_for("short") == ()
    text = "The court rules in 30-2026-01234567-CU-BC-CJC as follows. " * 5
    assert gate.evidence_for(text) == (text,)
    assert not gate.refuted("30-2026-01234567-CU-BC-CJC", gate.evidence_for(text))
    assert gate.refuted("30-2026-09999999-CU-BC-CJC", gate.evidence_for(text))


# ---------------------------------------------------------------------------
# Deadline pool
# ---------------------------------------------------------------------------


def _hang(_x):
    time.sleep(60)


def _boom(_x):
    raise ValueError("bad pdf")


def _die(_x):
    os._exit(3)


def test_run_with_deadline_kills_a_hanging_job() -> None:
    start = time.monotonic()
    jobs = [("fast", (1,)), ("hang", (2,)), ("fast2", (3,))]

    def fn(x):
        if x == 2:
            _hang(x)
        return x * 10

    results = {
        name: (status, payload)
        for name, status, payload in audit.run_with_deadline(
            jobs, fn, workers=2, timeout=1.0
        )
    }
    assert results == {
        "fast": ("ok", 10),
        "hang": ("timeout", None),
        "fast2": ("ok", 30),
    }
    assert time.monotonic() - start < 15


def test_run_with_deadline_reports_raise_and_crash() -> None:
    results = {
        name: (status, payload)
        for name, status, payload in audit.run_with_deadline(
            [("boom", (1,))], _boom, workers=1, timeout=10
        )
    }
    assert results["boom"] == ("error", "ValueError: bad pdf")
    results = list(
        audit.run_with_deadline([("die", (1,))], _die, workers=1, timeout=10)
    )
    assert results == [("die", "error", "worker exited with code 3")]


# ---------------------------------------------------------------------------
# County audit end to end (stubbed S3 + DB)
# ---------------------------------------------------------------------------


def test_hanging_extractor_counts_timeout_and_names_the_key() -> None:
    objects = {
        "ok.pdf": b"%PDF-ok",
        "stall.pdf": b"%PDF-stall",
        "scan.pdf": b"%PDF-scan",
    }

    def extract(content):
        if content == b"%PDF-stall":
            time.sleep(60)  # a PDF pdfplumber never finishes
        if content == b"%PDF-scan":
            return "tiny"
        return LONG + " 30-2026-01 "

    rows = {
        "Orange": [
            _r("ok.pdf", 1, "30-2026-01"),
            _r("ok.pdf", 2, "30-2026-99"),
            _r("stall.pdf", 3, "30-2026-02"),
            _r("stall.pdf", 4, "30-2026-03"),
            _r("scan.pdf", 5, "30-2026-04"),
        ]
    }
    lines: list[str] = []
    start = time.monotonic()
    code = audit.run(
        _args("Orange", "--timeout", "1", "--workers", "3"),
        rows,
        _gate(objects, extract),
        None,
        out=lines.append,
    )
    assert time.monotonic() - start < 15
    assert code == 1  # one unsupported ruling

    assert "TIMEOUT Orange key=stall.pdf rulings=2 after=1s" in lines
    assert any(
        line.startswith("UNSUPPORTED Orange doc=d-ok.pdf ruling=r2 case=30-2026-99")
        for line in lines
    )
    summary = json.loads(
        next(line for line in lines if line.startswith("SUMMARY Orange: ")).split(
            ": ", 1
        )[1]
    )
    assert {k: summary[k] for k in audit.VERDICTS} == {
        "supported": 1,
        "unsupported": 1,
        "no_text": 1,
        "timeout": 2,
        "error": 0,
    }
    assert summary["timeout_keys"] == ["stall.pdf"]
    assert summary["keys"] == 3
    assert summary["unsupported_rows"] == [
        {
            "document_id": "d-ok.pdf",
            "ruling_id": "r2",
            "case_number": "30-2026-99",
            "s3_key": "ok.pdf",
        }
    ]


def test_all_supported_exits_zero_with_errors_reported() -> None:
    objects = {"a.pdf": b"%PDF-a"}
    rows = {
        "Riverside": [_r("a.pdf", 1, "30-2026-01"), _r("gone.pdf", 2, "30-2026-01")]
    }
    lines: list[str] = []
    code = audit.run(
        _args("--county", "Riverside"), rows, _gate(objects), None, out=lines.append
    )
    assert code == 0
    assert any(
        line.startswith("ERROR Riverside key=gone.pdf rulings=1 error=KeyError")
        for line in lines
    )
    summary = json.loads(lines[-1].split(": ", 1)[1])
    assert (
        summary["supported"] == 1
        and summary["error"] == 1
        and summary["error_keys"] == ["gone.pdf"]
    )


def test_keys_filter_limit_and_progress() -> None:
    objects = {f"k{i}.pdf": b"%PDF" for i in range(6)}
    rows = {"Orange": [_r(f"k{i}.pdf", i, "30-2026-01") for i in range(6)]}
    lines: list[str] = []
    only = {"k1.pdf", "k2.pdf", "k3.pdf", "k4.pdf"}
    args = _args("Orange", "--limit", "3", "--progress-every", "1")
    assert audit.run(args, rows, _gate(objects), only, out=lines.append) == 0
    assert lines[0].startswith("START Orange: 3 keys")
    assert [
        line.split(":")[1].split()[0] for line in lines if line.startswith("PROGRESS")
    ] == ["1/3", "2/3"]
    summary = json.loads(lines[-1].split(": ", 1)[1])
    assert summary["keys"] == 3 and summary["supported"] == 3


def test_group_by_key_sorted_and_limited() -> None:
    rows = [_r("b", 1, "C"), _r("a", 2, "C"), _r("b", 3, "C"), _r("c", 4, "C")]
    grouped = audit.group_by_key(rows, None, 2)
    assert list(grouped) == ["a", "b"]
    assert [r.ruling_id for r in grouped["b"]] == ["r1", "r3"]
    assert list(audit.group_by_key(rows, {"c"}, None)) == ["c"]


# ---------------------------------------------------------------------------
# Arguments and key lists
# ---------------------------------------------------------------------------


def test_parse_args_counties() -> None:
    assert _args().all_counties == ["Orange", "Riverside"]
    assert _args("Riverside").all_counties == ["Riverside"]
    assert _args(
        "Orange", "--county", "Riverside", "--county", "Orange"
    ).all_counties == ["Orange", "Riverside"]
    with pytest.raises(SystemExit):
        _args("--workers", "0")


def test_load_key_list_s3_and_local(tmp_path) -> None:
    seen: list[tuple[str, str]] = []

    def s3_get(bucket: str, key: str) -> bytes:
        seen.append((bucket, key))
        return b"orange/a.pdf\n\n# comment\n orange/b.pdf \n"

    assert audit.load_key_list("s3://bkt/lists/keys.txt", s3_get) == {
        "orange/a.pdf",
        "orange/b.pdf",
    }
    assert seen == [("bkt", "lists/keys.txt")]
    local = tmp_path / "keys.txt"
    local.write_text("x.pdf\ny.pdf\n")
    assert audit.load_key_list(str(local), s3_get) == {"x.pdf", "y.pdf"}
    with pytest.raises(ValueError):
        audit.load_key_list("s3://bucket-only", s3_get)


def test_fetch_county_rows_uses_one_connection(monkeypatch) -> None:
    executed: list[tuple] = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            executed.append(params)

        def fetchall(self):
            county = executed[-1][0]
            return [("k.pdf", "d1", "r1", f"{county}-1")]

    class Conn(Cur):
        def cursor(self):
            return Cur()

    class FakePsycopg:
        @staticmethod
        def connect(url):
            assert url == "postgres://x"
            return Conn()

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)
    rows = audit.fetch_county_rows("postgres://x", ["Orange", "Riverside"])
    assert executed == [("Orange",), ("Riverside",)]
    assert rows["Riverside"] == [audit.Ruling("k.pdf", "d1", "r1", "Riverside-1")]


def test_main_wires_inputs(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        audit,
        "fetch_county_rows",
        lambda url, counties: {c: [_r("a.pdf", 1, "30-2026-01")] for c in counties},
    )
    monkeypatch.setattr(audit, "production_gate", lambda: _gate({"a.pdf": b"%PDF"}))
    assert audit.main(["Orange"]) == 0


# ---------------------------------------------------------------------------
# No-text PDF mode (#4983)
# ---------------------------------------------------------------------------


def test_county_prefix_and_raw_pdf_keys() -> None:
    assert audit.county_prefix("Santa Clara") == "ca/santa_clara/"
    assert audit.is_raw_pdf_key("ca/fresno/superior/raw/abc.pdf")
    assert audit.is_raw_pdf_key("ca/fresno/superior/raw/2026/03/17/u-u-i-d.pdf")
    assert not audit.is_raw_pdf_key("ca/fresno/superior/raw/abc.html")
    assert not audit.is_raw_pdf_key("ca/fresno/superior/meta/abc.pdf")


def test_no_text_mode_reports_scanned_pdfs_with_ruling_counts() -> None:
    gate = _gate(
        {
            "ca/x/c/raw/scan.pdf": b"%PDF1",
            "ca/x/c/raw/short.pdf": b"%PDF2",
            "ca/x/c/raw/text.pdf": b"%PDF3",
        },
        extract_pdf=lambda content: {b"%PDF1": "", b"%PDF2": "  tiny  "}.get(
            content, LONG
        ),
        page_count=lambda content: {b"%PDF1": 22, b"%PDF2": 1}.get(content, 99),
    )
    lines: list[str] = []
    reports = audit.run_no_text(
        _args("--no-text-pdfs", "Riverside", "--workers", "2"),
        {
            "Riverside": [
                "ca/x/c/raw/scan.pdf",
                "ca/x/c/raw/short.pdf",
                "ca/x/c/raw/text.pdf",
                "ca/x/c/raw/missing.pdf",
            ]
        },
        {"ca/x/c/raw/short.pdf": 3},
        gate,
        None,
        out=lines.append,
    )
    summary = reports[0].summary()
    assert summary["pdf_keys"] == 4
    assert summary["no_text"] == 2
    assert summary["no_text_zero_rulings"] == 1
    assert summary["no_text_rulings"] == 3
    assert summary["no_text_over_10_pages"] == 1
    assert summary["error"] == 1
    assert "NO_TEXT Riverside key=ca/x/c/raw/short.pdf chars=4 pages=1 rulings=3" in lines
    assert "NO_TEXT Riverside key=ca/x/c/raw/scan.pdf chars=0 pages=22 rulings=0" in lines
    assert audit.no_text_key_list(reports) == (
        "ca/x/c/raw/scan.pdf\nca/x/c/raw/short.pdf\n"
    )


def test_no_text_mode_honours_keys_and_limit() -> None:
    gate = _gate({"a.pdf": b"%PDF", "b.pdf": b"%PDF"}, extract_pdf=lambda c: "")
    reports = audit.run_no_text(
        _args("--no-text-pdfs", "Fresno", "--limit", "1"),
        {"Fresno": ["b.pdf", "a.pdf", "c.pdf"]},
        {},
        gate,
        {"a.pdf", "b.pdf"},
        out=lambda line: None,
    )
    assert [k for k, *_ in reports[0].no_text] == ["a.pdf"]


def test_write_keys_needs_no_text_mode_and_s3_uri() -> None:
    with pytest.raises(SystemExit):
        _args("--write-keys", "s3://b/k")
    with pytest.raises(SystemExit):
        _args("--no-text-pdfs", "--write-keys", "local.txt")
    assert _args("--no-text-pdfs", "--write-keys", "s3://b/k").write_keys == "s3://b/k"


def test_list_county_pdf_keys_and_ruling_counts(monkeypatch) -> None:
    class Paginator:
        def paginate(self, Bucket, Prefix):
            assert Bucket == "bkt"
            yield {
                "Contents": [
                    {"Key": f"{Prefix}sup/raw/a.pdf"},
                    {"Key": f"{Prefix}sup/raw/a.html"},
                ]
            }

    class S3:
        def get_paginator(self, name):
            return Paginator()

    assert audit.list_county_pdf_keys(S3(), "bkt", ["San Francisco"]) == {
        "San Francisco": ["ca/san_francisco/sup/raw/a.pdf"]
    }

    executed: list[tuple] = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            executed.append(params)

        def fetchall(self):
            return [(executed[-1][0].replace("%", "k.pdf"), 2)]

    class Conn(Cur):
        def cursor(self):
            return Cur()

    class FakePsycopg:
        @staticmethod
        def connect(url):
            return Conn()

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)
    counts = audit.fetch_ruling_counts("postgres://x", ["Fresno"])
    assert executed == [("ca/fresno/%",)]
    assert counts == {"ca/fresno/k.pdf": 2}


def test_main_no_text_writes_key_list(monkeypatch) -> None:
    puts: list[dict] = []

    class S3:
        def put_object(self, **kw):
            puts.append(kw)

    class FakeBoto3:
        @staticmethod
        def client(name):
            return S3()

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setattr(
        audit, "list_county_pdf_keys", lambda s3, b, counties: {"Fresno": ["a.pdf"]}
    )
    monkeypatch.setattr(audit, "fetch_ruling_counts", lambda url, counties: {})
    monkeypatch.setattr(
        audit,
        "production_gate",
        lambda: _gate({"a.pdf": b"%PDF"}, extract_pdf=lambda c: ""),
    )
    assert (
        audit.main(["Fresno", "--no-text-pdfs", "--write-keys", "s3://bkt/tmp/k.txt"])
        == 0
    )
    assert puts == [{"Bucket": "bkt", "Key": "tmp/k.txt", "Body": b"a.pdf\n"}]


def test_no_text_mode_min_pages_keeps_long_scans_only() -> None:
    """``--min-pages 11`` lists the scans the old 10-page OCR cap cut short
    (#4986), for a reingest key list."""
    gate = _gate(
        {"long.pdf": b"%PDF1", "short.pdf": b"%PDF2", "text.pdf": b"%PDF3"},
        extract_pdf=lambda c: "" if c != b"%PDF3" else LONG,
        page_count=lambda c: {b"%PDF1": 22, b"%PDF2": 3}.get(c, 40),
    )
    lines: list[str] = []
    reports = audit.run_no_text(
        _args("--no-text-pdfs", "Santa Clara", "--min-pages", "11"),
        {"Santa Clara": ["long.pdf", "short.pdf", "text.pdf"]},
        {"long.pdf": 1},
        gate,
        None,
        out=lines.append,
    )
    assert audit.no_text_key_list(reports) == "long.pdf\n"
    summary = reports[0].summary()
    assert summary["no_text"] == 1
    assert summary["no_text_over_10_pages"] == 1
    assert "NO_TEXT Santa Clara key=long.pdf chars=0 pages=22 rulings=1" in lines


def test_probe_reads_page_count_only_without_evidence() -> None:
    counted: list[bytes] = []

    def page_count(content: bytes) -> int:
        counted.append(content)
        return 5

    gate = _gate(
        {"a.pdf": b"%PDF1", "b.pdf": b"%PDF2"},
        extract_pdf=lambda c: LONG if c == b"%PDF1" else "",
        page_count=page_count,
    )
    assert audit.probe_key_text("a.pdf", gate).pages == 0
    assert audit.probe_key_text("b.pdf", gate).pages == 5
    assert counted == [b"%PDF2"]
