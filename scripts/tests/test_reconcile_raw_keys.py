"""Tests for scripts/reconcile_raw_keys.py (#5014).

A fake in-memory S3 stands in for boto3: list/get/put.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_raw_keys as rec

BUCKET = "test-archive"
LA = "ca/los_angeles/superior_court/raw/"
SD = "ca/san_diego/superior_court/raw/"
SC = "ca/santa_clara/superior_court/raw/"
GOV = "ca/statewide/governor/raw/"


def _h(c: str) -> str:
    return c * 64


def _lm(day: str) -> datetime:
    return datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=UTC)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self, objects: dict[str, tuple[datetime, bytes]]) -> None:
        self.objects = objects
        self.puts: dict[str, bytes] = {}

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        # two pages, to exercise pagination
        for part in (keys[:2], keys[2:]):
            yield {
                "Contents": [
                    {"Key": k, "LastModified": self.objects[k][0]} for k in part
                ]
            }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": _Body(self.objects[Key][1])}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.puts[f"s3://{Bucket}/{Key}"] = Body


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3(
        {
            f"{LA}{_h('a')}.html": (_lm("2026-05-12"), b""),  # in documents
            f"{LA}{_h('b')}.html": (_lm("2026-05-13"), b""),  # unexplained
            f"{LA}{_h('c')}.html": (_lm("2026-06-02"), b""),  # unexplained
            f"{LA}{_h('d')}.html": (_lm("2026-06-03"), b""),  # unexplained
            f"{LA}{_h('e')}.html": (_lm("2026-10-05"), b""),  # outside window
            f"{SD}{_h('1')}.pdf": (_lm("2026-05-20"), b""),  # validation fail
            f"{SD}{_h('2')}.pdf": (_lm("2026-05-21"), b""),  # all-null guard
            f"{SC}{_h('3')}.pdf": (_lm("2026-05-22"), b"placeholder"),
            f"{SC}{_h('4')}.pdf": (_lm("2026-05-22"), b"real ruling"),
            f"{GOV}{_h('5')}.html": (_lm("2026-05-23"), b""),
            f"{LA}2026/03/04/u.pdf": (_lm("2026-03-04"), b""),  # dated layout
            f"{LA}notes.txt": (_lm("2026-05-12"), b""),  # other layout
            "ca/los_angeles/superior_court/text/x.txt": (_lm("2026-05-12"), b""),
        }
    )


@pytest.fixture
def explained() -> rec.Explained:
    return rec.Explained(
        documents={f"{LA}{_h('a')}.html"},
        validation={f"{SD}{_h('1')}.pdf"},
        dq_metrics={f"{SD}{_h('2')}.pdf"},
    )


def _args(*argv: str):
    return rec.parse_args(["--bucket", BUCKET, *argv])


def _summaries(lines: list[str]) -> dict[str, str]:
    return {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in lines
        if line.startswith("SUMMARY")
    }


def test_classify_order(explained: rec.Explained) -> None:
    assert rec.classify(f"{LA}{_h('a')}.html", explained) == "in_documents"
    assert rec.classify(f"{SD}{_h('1')}.pdf", explained) == "validation_only"
    assert rec.classify(f"{SD}{_h('2')}.pdf", explained) == "dq_only"
    assert rec.classify(f"{GOV}{_h('5')}.html", explained) == "skip_governor"
    assert rec.classify(f"{LA}{_h('b')}.html", explained) == "unexplained"


def test_partition_keys_window_and_layouts(s3: FakeS3) -> None:
    keys, other = rec.partition_keys(
        rec.list_raw_objects(s3, BUCKET, "ca/"),
        since=date(2026, 5, 6),
        until=date(2026, 6, 19),
    )
    assert other == {"dated": 1, "other_layout": 1, "not_raw": 1, "outside_window": 1}
    assert {k.county for k in keys} == {
        "los_angeles",
        "san_diego",
        "santa_clara",
        "statewide",
    }
    assert f"{LA}{_h('e')}.html" not in {k.key for k in keys}


def test_partition_keys_county_filter(s3: FakeS3) -> None:
    keys, _ = rec.partition_keys(
        rec.list_raw_objects(s3, BUCKET, "ca/"), counties={"san_diego"}
    )
    assert sorted(k.key for k in keys) == [f"{SD}{_h('1')}.pdf", f"{SD}{_h('2')}.pdf"]


def test_raw_key_day_handles_naive_and_offset_times() -> None:
    naive = rec.RawKey("k", "c", datetime(2026, 5, 12, 23, 0))
    assert naive.day == "2026-05-12"
    pst = timezone(timedelta(hours=-8))
    assert rec.RawKey("k", "c", datetime(2026, 5, 12, 20, 0, tzinfo=pst)).day == (
        "2026-05-13"
    )


def test_run_reports_and_writes_chunked_key_lists(
    s3: FakeS3, explained: rec.Explained
) -> None:
    lines: list[str] = []
    stem = f"s3://{BUCKET}/tmp/keys/5014"
    args = _args(
        "--since",
        "2026-05-06",
        "--until",
        "2026-06-19",
        "--write-keys",
        stem,
        "--chunk-size",
        "2",
    )
    reports = rec.run(args, s3, explained, out=lines.append)

    la = reports["los_angeles"].summary()
    assert la["in_documents"] == 1
    assert la["unexplained"] == 3
    assert la["raw"] == 4
    assert la["unexplained_by_month"] == {"2026-05": 1, "2026-06": 2}
    sd = reports["san_diego"].summary()
    assert (sd["validation_only"], sd["dq_only"], sd["unexplained"]) == (1, 1, 0)
    assert reports["statewide"].summary()["skip_governor"] == 1
    # without --classify-placeholders both Santa Clara keys stay unexplained
    assert reports["santa_clara"].summary()["unexplained"] == 2

    assert s3.puts[f"{stem}-los_angeles-01.txt"] == (
        f"{LA}{_h('b')}.html\n{LA}{_h('c')}.html\n".encode()
    )
    assert s3.puts[f"{stem}-los_angeles-02.txt"] == f"{LA}{_h('d')}.html\n".encode()
    assert f"{stem}-santa_clara.txt" in s3.puts
    assert not any("san_diego" in uri for uri in s3.puts)

    summaries = _summaries(lines)
    assert '"unexplained": 5' in summaries["SUMMARY_ALL"]
    assert any(line.startswith("UNEXPLAINED_DAYS los_angeles:") for line in lines)
    assert any(line.startswith("WROTE_KEYS los_angeles 2 -> ") for line in lines)


def test_run_classifies_santa_clara_placeholders(
    s3: FakeS3, explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rec, "is_sc_placeholder_pdf", lambda body: body == b"placeholder"
    )
    args = _args("--county", "santa_clara", "--classify-placeholders")
    reports = rec.run(args, s3, explained, out=lambda _line: None)
    sc = reports["santa_clara"].summary()
    assert (sc["skip_sc_placeholder"], sc["unexplained"]) == (1, 1)


def test_run_placeholder_check_error_leaves_key_unexplained(
    s3: FakeS3, explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_body: bytes) -> bool:
        raise RuntimeError("bad pdf")

    monkeypatch.setattr(rec, "is_sc_placeholder_pdf", boom)
    lines: list[str] = []
    args = _args("--county", "santa_clara", "--classify-placeholders")
    reports = rec.run(args, s3, explained, out=lines.append)
    assert reports["santa_clara"].summary()["unexplained"] == 2
    assert sum(line.startswith("PLACEHOLDER_CHECK_ERROR") for line in lines) == 2


def test_is_sc_placeholder_pdf_uses_worker_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import courts.ca.sc_tentatives as sc
    import ingestion.llm_extract as le

    monkeypatch.setattr(le, "extract_text_layer", lambda body: body.decode())
    monkeypatch.setattr(sc, "is_placeholder_notice", lambda text: text == "notice")
    assert rec.is_sc_placeholder_pdf(b"notice") is True
    assert rec.is_sc_placeholder_pdf(b"ruling") is False


def test_chunked_and_uris() -> None:
    assert rec.chunked(["a", "b", "c"], 0) == [["a", "b", "c"]]
    assert rec.chunked(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    assert rec.key_list_uris("s", "la", 1) == ["s-la.txt"]
    assert rec.key_list_uris("s", "la", 2) == ["s-la-01.txt", "s-la-02.txt"]


def test_write_text_local(tmp_path) -> None:
    path = tmp_path / "keys.txt"
    rec.write_text(None, str(path), "a\n")
    assert path.read_text() == "a\n"


def test_parse_args_validation() -> None:
    with pytest.raises(SystemExit):
        rec.parse_args(["--since", "2026-06-19", "--until", "2026-05-06"])
    with pytest.raises(SystemExit):
        rec.parse_args(["--prefix", "ca"])
    args = rec.parse_args(["--county", "los_angeles", "--county", "san_diego"])
    assert args.county == ["los_angeles", "san_diego"]
    assert args.since is None


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert rec.main([]) == 2


class _FakeCursor:
    def __init__(self, results: list[list[tuple[str]]]) -> None:
        self.results = results
        self.queries: list[tuple[str, tuple[str, ...]]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        self.queries.append((sql, params))

    def fetchall(self) -> list[tuple[str]]:
        return self.results.pop(0)


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self.cur = cur

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self.cur


def test_fetch_explained_reads_all_three_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cur = _FakeCursor([[("k1",)], [("k2",)], [("k3",)]])
    fake = types.SimpleNamespace(connect=lambda url: _FakeConn(cur))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    explained = rec.fetch_explained("postgres://x", "ca/los_angeles/")
    assert explained == rec.Explained({"k1"}, {"k2"}, {"k3"})
    assert [q[1] for q in cur.queries] == [(r"ca/los\_angeles/%",)] * 3
    assert "derived.documents" in cur.queries[0][0]
    assert "validation_results" in cur.queries[1][0]
    assert "data_quality_metrics" in cur.queries[2][0]


def test_main_runs_with_database(
    monkeypatch: pytest.MonkeyPatch, s3: FakeS3, explained: rec.Explained
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda name: s3)
    )
    monkeypatch.setattr(rec, "fetch_explained", lambda url, prefix: explained)
    assert rec.main(["--bucket", BUCKET]) == 0
