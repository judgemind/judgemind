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


def _gate(objects: dict[str, bytes], extract_pdf=None) -> audit.Gate:
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
    assert gate.extract_pdf is llm_extract.extract_text_from_pdf
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
