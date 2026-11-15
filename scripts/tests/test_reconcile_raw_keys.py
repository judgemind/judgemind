"""Tests for scripts/reconcile_raw_keys.py (#5014, #5015).

A fake in-memory S3 stands in for boto3 (list with and without a delimiter,
get, put); a fake connection records the SQL for the database side.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Self

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_raw_keys as rec

BUCKET = "test-archive"
LA = "ca/los_angeles/superior_court/raw/"
SD = "ca/san_diego/superior_court/raw/"
SC = "ca/santa_clara/superior_court/raw/"
GOV = "ca/statewide/governor/raw/"
FED = "federal/federal/courtlistener/raw/"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _h(c: str) -> str:
    return c * 64


def _lm(day: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=UTC)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self, objects: dict[str, tuple[datetime, bytes]]) -> None:
        self.objects = objects
        self.puts: dict[str, bytes] = {}
        self.listed: list[tuple[str, str | None]] = []
        self.gets: list[str] = []

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str, Delimiter: str | None = None):
        self.listed.append((Prefix, Delimiter))
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if Delimiter:
            prefixes = sorted(
                {
                    Prefix + k[len(Prefix) :].split("/", 1)[0] + "/"
                    for k in keys
                    if "/" in k[len(Prefix) :]
                }
            )
            direct = [k for k in keys if "/" not in k[len(Prefix) :]]
            yield {
                "CommonPrefixes": [{"Prefix": p} for p in prefixes],
                "Contents": [
                    {"Key": k, "LastModified": self.objects[k][0]} for k in direct
                ],
            }
            return
        # two pages, to exercise pagination
        for part in (keys[:2], keys[2:]):
            yield {
                "Contents": [
                    {"Key": k, "LastModified": self.objects[k][0]} for k in part
                ]
            }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.gets.append(Key)
        return {"Body": _Body(self.objects[Key][1])}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts[f"s3://{Bucket}/{Key}"] = Body


@pytest.fixture
def s3() -> FakeS3:
    return FakeS3(
        {
            f"{LA}{_h('a')}.html": (_lm("2026-05-12"), b""),  # in documents
            f"{LA}{_h('b')}.html": (_lm("2026-05-13"), b""),  # unrepresented
            f"{LA}{_h('c')}.html": (_lm("2026-06-02"), b""),  # unrepresented
            f"{LA}{_h('d')}.html": (_lm("2026-06-03"), b""),  # unrepresented
            f"{LA}{_h('e')}.html": (_lm("2026-11-15", 9), b""),  # pending (3h old)
            f"{SD}{_h('1')}.pdf": (_lm("2026-05-20"), b""),  # validation fail
            f"{SD}{_h('2')}.pdf": (_lm("2026-05-21"), b""),  # all-null guard
            f"{SD}{_h('6')}.pdf": (_lm("2026-05-21"), b""),  # ingest_skips
            f"{SC}{_h('3')}.pdf": (_lm("2026-05-22"), b"placeholder"),
            f"{SC}{_h('4')}.pdf": (_lm("2026-05-22"), b"real ruling"),
            f"{GOV}{_h('5')}.html": (_lm("2026-05-23"), b""),  # governor prefix
            f"{FED}{_h('7')}.txt": (_lm("2026-06-01"), b""),  # unrepresented
            f"{LA}2026/03/04/u.pdf": (_lm("2026-03-04"), b""),  # dated layout
            f"{LA}notes.txt": (_lm("2026-05-12"), b""),  # other layout
            "ca/los_angeles/superior_court/text/x.txt": (_lm("2026-05-12"), b""),
            "tmp/keys/old.txt": (_lm("2026-05-12"), b""),  # excluded prefix
            "llm-cache/x/y.json": (_lm("2026-05-12"), b""),  # excluded prefix
            "README": (_lm("2026-05-12"), b""),  # stray top-level object
        }
    )


@pytest.fixture
def explained() -> rec.Explained:
    return rec.Explained(
        documents={f"{LA}{_h('a')}.html"},
        skips={f"{SD}{_h('6')}.pdf": "no_extractable_text"},
        validation_fail={f"{SD}{_h('1')}.pdf"},
        all_null_guard={f"{SD}{_h('2')}.pdf"},
    )


def _args(*argv: str) -> Any:
    return rec.parse_args(["--bucket", BUCKET, *argv])


def _result(lines: list[str]) -> dict[str, Any]:
    line = next(x for x in lines if x.startswith("RECONCILE_RESULT "))
    return json.loads(line[len("RECONCILE_RESULT ") :])


# -- classification ---------------------------------------------------------


def _raw(key: str, lm: datetime = OLD) -> rec.RawKey:
    m = rec.CONTENT_KEY_RE.match(key)
    assert m
    return rec.RawKey(key, m["state"], m["county"], lm)


def test_classify_order_and_reasons(explained: rec.Explained) -> None:
    g = timedelta(hours=6)

    def c(key: str, lm: datetime = OLD) -> tuple[str, str | None]:
        return rec.classify(_raw(key, lm), explained, now=NOW, grace=g)

    assert c(f"{LA}{_h('a')}.html") == ("represented", None)
    assert c(f"{SD}{_h('6')}.pdf") == ("skipped", "no_extractable_text")
    assert c(f"{SD}{_h('1')}.pdf") == ("skipped", "validation_fail")
    assert c(f"{SD}{_h('2')}.pdf") == ("skipped", "all_null_guard")
    assert c(f"{GOV}{_h('5')}.html") == ("skipped", "governor_bio_route")
    assert c(f"{LA}{_h('b')}.html") == ("unrepresented", None)
    assert c(f"{LA}{_h('b')}.html", _lm("2026-11-15", 7)) == ("pending", None)
    # a represented key is represented however fresh it is
    assert c(f"{LA}{_h('a')}.html", _lm("2026-11-15", 11)) == ("represented", None)


def test_skip_record_beats_validation_fail() -> None:
    key = f"{SD}{_h('9')}.pdf"
    ex = rec.Explained(skips={key: "zero_rulings_no_text"}, validation_fail={key})
    assert rec.classify(_raw(key), ex, now=NOW, grace=timedelta(0)) == (
        "skipped",
        "zero_rulings_no_text",
    )


def test_partition_keys_window_layouts_and_dated_scopes(s3: FakeS3) -> None:
    keys, other, dated = rec.partition_keys(
        rec.list_raw_objects(s3, BUCKET, "ca/"),
        since=date(2026, 5, 6),
        until=date(2026, 6, 19),
    )
    assert other == {"dated": 1, "other_layout": 1, "not_raw": 1, "outside_window": 1}
    assert dated == {"ca/los_angeles": 1}
    assert {k.scope for k in keys} == {
        "ca/los_angeles",
        "ca/san_diego",
        "ca/santa_clara",
        "ca/statewide",
    }


def test_partition_keys_county_filter(s3: FakeS3) -> None:
    keys, _, dated = rec.partition_keys(
        rec.list_raw_objects(s3, BUCKET, "ca/"), counties={"san_diego"}
    )
    assert sorted(k.key for k in keys) == sorted(
        [f"{SD}{_h('1')}.pdf", f"{SD}{_h('2')}.pdf", f"{SD}{_h('6')}.pdf"]
    )
    assert dated == {}


def test_raw_key_day_handles_naive_and_offset_times() -> None:
    naive = rec.RawKey("k", "ca", "c", datetime(2026, 5, 12, 23, 0))
    assert naive.day == "2026-05-12"
    pst = timezone(timedelta(hours=-8))
    assert rec.RawKey("k", "ca", "c", datetime(2026, 5, 12, 20, 0, tzinfo=pst)).day == (
        "2026-05-13"
    )


# -- listing ----------------------------------------------------------------


def test_listing_skips_excluded_prefixes_and_lists_per_county(s3: FakeS3) -> None:
    objects = rec.list_all_raw_objects(
        s3, BUCKET, "", rec.DEFAULT_EXCLUDE_PREFIXES, workers=4
    )
    keys = {k for k, _ in objects}
    assert f"{FED}{_h('7')}.txt" in keys
    assert not any(k.startswith(("tmp/", "llm-cache/")) for k in keys)
    assert "README" in keys  # stray, counted as not_raw
    full_listings = sorted(p for p, d in s3.listed if d is None)
    assert full_listings == [
        "ca/los_angeles/",
        "ca/san_diego/",
        "ca/santa_clara/",
        "ca/statewide/",
        "federal/federal/",
    ]


def test_listing_a_county_prefix_lists_it_directly(s3: FakeS3) -> None:
    units, stray = rec.listing_units(s3, BUCKET, "ca/san_diego/", ())
    assert units == ["ca/san_diego/"]
    assert stray == []


def test_full_run_reads_no_objects(s3: FakeS3, explained: rec.Explained) -> None:
    rec.run(_args(), s3, explained, out=lambda _l: None, now=NOW)
    assert s3.gets == []


# -- run --------------------------------------------------------------------


def test_run_counts_per_scope_and_result(s3: FakeS3, explained: rec.Explained) -> None:
    lines: list[str] = []
    out = rec.run(_args(), s3, explained, out=lines.append, now=NOW)
    reports = out["reports"]
    la = reports["ca/los_angeles"].summary()
    assert (la["represented"], la["unrepresented"], la["pending"], la["raw"]) == (
        1,
        3,
        1,
        5,
    )
    assert la["dated_excluded"] == 1
    assert la["unrepresented_by_month"] == {"2026-05": 1, "2026-06": 2}
    sd = reports["ca/san_diego"].summary()
    assert sd["skipped"] == 3
    assert sd["skipped_by_reason"] == {
        "all_null_guard": 1,
        "no_extractable_text": 1,
        "validation_fail": 1,
    }
    assert reports["ca/statewide"].summary()["skipped_by_reason"] == {
        "governor_bio_route": 1
    }
    assert reports["federal/federal"].summary()["unrepresented"] == 1
    # without --classify-placeholders both Santa Clara keys stay unrepresented
    assert reports["ca/santa_clara"].summary()["unrepresented"] == 2

    result = _result(lines)
    assert result["unrepresented"] == 6
    assert result["represented"] == 1
    assert result["skipped"] == 4
    assert result["pending"] == 1
    assert result["action"] == "report_only"
    assert result["issue_needed"] is False
    assert result["run_id"] is None
    assert result["top_scopes"][0] == ["ca/los_angeles", 3]
    assert any(x.startswith("UNREPRESENTED_DAYS ca/los_angeles:") for x in lines)


def test_run_writes_chunked_key_lists(s3: FakeS3, explained: rec.Explained) -> None:
    lines: list[str] = []
    stem = f"s3://{BUCKET}/tmp/keys/5014"
    args = _args("--prefix", "ca/", "--write-keys", stem, "--chunk-size", "2")
    rec.run(args, s3, explained, out=lines.append, now=NOW)
    assert s3.puts[f"{stem}-ca_los_angeles-01.txt"] == (
        f"{LA}{_h('b')}.html\n{LA}{_h('c')}.html\n".encode()
    )
    assert s3.puts[f"{stem}-ca_los_angeles-02.txt"] == f"{LA}{_h('d')}.html\n".encode()
    assert f"{stem}-ca_santa_clara.txt" in s3.puts
    assert not any("san_diego" in uri or "federal" in uri for uri in s3.puts)
    assert any(x.startswith("WROTE_KEYS ca/los_angeles 2 -> ") for x in lines)


def test_run_classifies_santa_clara_placeholders(
    s3: FakeS3, explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rec, "is_sc_placeholder_pdf", lambda body: body == b"placeholder"
    )
    args = _args("--county", "santa_clara", "--classify-placeholders")
    out = rec.run(args, s3, explained, out=lambda _l: None, now=NOW)
    sc = out["reports"]["ca/santa_clara"].summary()
    assert (sc["skipped"], sc["unrepresented"]) == (1, 1)
    assert sc["skipped_by_reason"] == {"santa_clara_placeholder": 1}


def test_run_placeholder_check_error_leaves_key_unrepresented(
    s3: FakeS3, explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_body: bytes) -> bool:
        raise RuntimeError("bad pdf")

    monkeypatch.setattr(rec, "is_sc_placeholder_pdf", boom)
    lines: list[str] = []
    args = _args("--county", "santa_clara", "--classify-placeholders")
    out = rec.run(args, s3, explained, out=lines.append, now=NOW)
    assert out["reports"]["ca/santa_clara"].summary()["unrepresented"] == 2
    assert sum(x.startswith("PLACEHOLDER_CHECK_ERROR") for x in lines) == 2


def test_is_sc_placeholder_pdf_uses_worker_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub modules: the scripts-tests CI job does not install the
    # scraper-framework dependencies the real modules import.
    sc = types.SimpleNamespace(is_placeholder_notice=lambda text: text == "notice")
    le = types.SimpleNamespace(extract_text_layer=lambda body: body.decode())
    monkeypatch.setitem(sys.modules, "courts.ca.sc_tentatives", sc)
    monkeypatch.setitem(sys.modules, "ingestion.llm_extract", le)
    assert rec.is_sc_placeholder_pdf(b"notice") is True
    assert rec.is_sc_placeholder_pdf(b"ruling") is False


# -- threshold / action -----------------------------------------------------


@pytest.mark.parametrize(
    ("unrep", "previous", "reingest", "defer", "expected"),
    [
        (0, [(5, "reingest"), (5, "reingest")], True, False, "none"),
        (501, [], True, False, "over_threshold"),
        (500, [], True, False, "reingest"),
        # two earlier runs re-ingested and the keys came back
        (10, [(3, "reingest"), (7, "persistent")], True, False, "persistent"),
        (10, [(0, "none"), (7, "reingest")], True, False, "reingest"),
        (10, [(7, "reingest")], True, False, "reingest"),  # not enough history yet
        # runs that never re-ingested are not a failed heal
        (10, [(3, "reingest_deferred"), (7, "reingest")], True, False, "reingest"),
        (10, [(3, "reingest"), (900, "over_threshold")], True, False, "reingest"),
        (10, [(3, "report_only"), (7, "report_only")], True, False, "reingest"),
        (10, [(3, "reingest"), (7, "reingest")], False, False, "report_only"),
        (10, [(3, "reingest"), (7, "reingest")], True, True, "reingest_deferred"),
        (501, [(3, "reingest"), (7, "reingest")], True, True, "over_threshold"),
    ],
)
def test_decide_action(
    unrep: int,
    previous: list[tuple[int, str]],
    reingest: bool,
    defer: bool,
    expected: str,
) -> None:
    assert (
        rec.decide_action(
            unrep,
            previous,
            max_auto_reingest=500,
            persist_runs=3,
            reingest=reingest,
            defer=defer,
        )
        == expected
    )


def test_decide_action_without_history_never_persistent() -> None:
    assert (
        rec.decide_action(
            5,
            [(9, "reingest"), (9, "reingest")],
            max_auto_reingest=500,
            persist_runs=0,
            reingest=True,
            defer=False,
        )
        == "reingest"
    )


# -- database ---------------------------------------------------------------


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.db.queries.append((sql, params))
        if self.db.fail_on and self.db.fail_on in sql:
            raise RuntimeError(f"relation for {self.db.fail_on} does not exist")
        for needle, rows in self.db.results.items():
            if needle in sql:
                self._rows = list(rows)
                return
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeDb:
    def __init__(self, results: dict[str, list[tuple[Any, ...]]] | None = None) -> None:
        self.results = results or {}
        self.queries: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: str | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sql(self, needle: str) -> list[tuple[str, Any]]:
        return [q for q in self.queries if needle in q[0]]


def test_fetch_explained_reads_all_four_sources() -> None:
    db = FakeDb(
        {
            "derived.documents": [("k1",), (None,)],
            "validation_results": [("k2",)],
            "data_quality_metrics": [("k3",)],
            "ingest_skips": [("k4", "governor_bio_route")],
        }
    )
    ex = rec.fetch_explained(db)
    assert ex == rec.Explained({"k1"}, {"k4": "governor_bio_route"}, {"k2"}, {"k3"})
    assert "result = 'fail'" in db.sql("validation_results")[0][0]
    assert "non_ruling_pdf_dropped" in db.sql("data_quality_metrics")[0][0]


def test_fetch_explained_tolerates_missing_skips_table(capsys: Any) -> None:
    db = FakeDb({"derived.documents": [("k1",)]})
    db.fail_on = "ingest_skips"
    ex = rec.fetch_explained(db)
    assert ex.documents == {"k1"}
    assert ex.skips == {}
    assert db.rollbacks == 1
    assert "ingest_skips unreadable" in capsys.readouterr().out


def test_record_run_writes_run_and_scope_rows(
    s3: FakeS3, explained: rec.Explained
) -> None:
    db = FakeDb({"RETURNING run_id": [("run-1",)], "ORDER BY finished_at": []})
    lines: list[str] = []
    out = rec.run(_args("--record"), s3, explained, out=lines.append, now=NOW, conn=db)
    assert out["result"]["run_id"] == "run-1"
    assert out["result"]["action"] == "report_only"
    run_insert = db.sql("INSERT INTO telemetry.raw_reconcile_runs")[0][1]
    # raw, represented, skipped, unrepresented, pending, dated_excluded, action
    assert run_insert[1:8] == (12, 1, 4, 6, 1, 1, "report_only")
    scope_rows = {
        p[1]: p for _, p in db.sql("INSERT INTO telemetry.raw_reconcile_counts")
    }
    assert set(scope_rows) == {
        "ca/los_angeles",
        "ca/san_diego",
        "ca/santa_clara",
        "ca/statewide",
        "federal/federal",
    }
    la = scope_rows["ca/los_angeles"]
    assert la[2:8] == (5, 1, 0, 3, 1, 1)
    assert json.loads(la[9]) == {"2026-05-13": 1, "2026-06-02": 1, "2026-06-03": 1}
    assert json.loads(scope_rows["ca/san_diego"][8])["validation_fail"] == 1
    assert db.commits >= 1
    assert "RECORDED run_id=run-1" in lines


def _stub_script(tmp_path: Any) -> str:
    script = tmp_path / "reingest_from_s3.py"
    script.write_text("# stub\n")
    return str(script)


def test_persistent_still_reingests_and_files_an_issue(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    db = FakeDb(
        {
            "RETURNING run_id": [("run-2",)],
            "ORDER BY finished_at": [(4, "reingest"), (9, "persistent")],
        }
    )
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
    )
    result = _result(lines)
    assert result["action"] == "persistent"
    assert result["issue_needed"] is True
    assert result["previous_unrepresented"] == [4, 9]
    assert result["previous_actions"] == ["reingest", "persistent"]
    assert db.sql("ORDER BY finished_at")[0][1] == (2,)
    # new losses keep healing while the issue is open
    assert len(calls) == 2


def test_reingest_launch_failure_is_recorded_and_reported(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    db = FakeDb({"RETURNING run_id": [("run-6",)]})
    lines: list[str] = []

    def runner(cmd: list[str], check: bool) -> Any:
        raise OSError("exec format error")

    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=runner,
    )
    result = _result(lines)
    assert result["action"] == "reingest_failed"
    assert result["issue_needed"] is True
    assert "exec format error" in result["reingest_error"]
    update = db.sql("UPDATE telemetry.raw_reconcile_runs")[0][1]
    assert update[0] == "reingest_failed"


def test_missing_reingest_script_is_recorded_before_anything_runs(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    db = FakeDb({"RETURNING run_id": [("run-7",)]})
    lines: list[str] = []
    calls: list[Any] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", str(tmp_path / "nope.py")),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: calls.append(cmd),
    )
    result = _result(lines)
    assert result["action"] == "reingest_failed"
    run_insert = db.sql("INSERT INTO telemetry.raw_reconcile_runs")[0][1]
    assert run_insert[7] == "reingest_failed"
    assert calls == []
    assert s3.puts == {}


def test_over_threshold_files_an_issue_and_does_not_reingest(
    s3: FakeS3, explained: rec.Explained
) -> None:
    db = FakeDb({"RETURNING run_id": [("run-3",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--max-auto-reingest", "5"),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: calls.append(cmd),
    )
    result = _result(lines)
    assert result["action"] == "over_threshold"
    assert result["issue_needed"] is True
    assert calls == []
    assert s3.puts == {}


def test_reingest_writes_key_lists_and_runs_per_state(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    script = _stub_script(tmp_path)
    db = FakeDb({"RETURNING run_id": [("run-4",)]})
    calls: list[list[str]] = []
    lines: list[str] = []

    def runner(cmd: list[str], check: bool) -> Any:
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    rec.run(
        _args("--record", "--reingest", "--reingest-script", script),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=runner,
    )
    result = _result(lines)
    assert result["action"] == "reingest"
    assert [c[c.index("--prefix") + 1] for c in calls] == ["ca/", "federal/"]
    ca_cmd = calls[0]
    assert ca_cmd[1] == script
    assert ca_cmd[ca_cmd.index("--concurrency") + 1] == "4"
    uri = ca_cmd[ca_cmd.index("--s3-key-list") + 1]
    assert uri == f"s3://{BUCKET}/tmp/keys/raw-reconcile-20261115T1200Z-ca.txt"
    assert s3.puts[uri].decode().splitlines() == [
        f"{LA}{_h('b')}.html",
        f"{LA}{_h('c')}.html",
        f"{LA}{_h('d')}.html",
        f"{SC}{_h('3')}.pdf",
        f"{SC}{_h('4')}.pdf",
    ]
    assert "--write-failed-manifest" in ca_cmd
    assert result["reingest"][0] == {
        "prefix": "ca/",
        "keys": 5,
        "key_list": uri,
        "exit": 0,
    }
    update = db.sql("UPDATE telemetry.raw_reconcile_runs")[0][1]
    assert update[0] == "reingest"
    assert json.loads(update[1])["reingest"][1]["prefix"] == "federal/"


def test_reingest_deferred_runs_nothing(s3: FakeS3, explained: rec.Explained) -> None:
    db = FakeDb({"RETURNING run_id": [("run-5",)]})
    calls: list[Any] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--defer-reingest"),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: calls.append(cmd),
    )
    assert _result(lines)["action"] == "reingest_deferred"
    assert calls == []


def test_find_reingest_script_missing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rec, "DEFAULT_REINGEST_SCRIPTS", (str(tmp_path / "nope.py"),))
    with pytest.raises(FileNotFoundError):
        rec.find_reingest_script(None)


def test_reingest_groups_by_state() -> None:
    reports = {
        "ca/x": rec.ScopeReport("ca/x", unrepresented=[_raw(f"{LA}{_h('b')}.html")]),
        "federal/federal": rec.ScopeReport(
            "federal/federal", unrepresented=[_raw(f"{FED}{_h('7')}.txt")]
        ),
    }
    assert rec.reingest_groups(reports) == {
        "ca/": [f"{LA}{_h('b')}.html"],
        "federal/": [f"{FED}{_h('7')}.txt"],
    }


def test_totals_of_empty_run() -> None:
    assert rec.totals({}) == dict.fromkeys(
        ("represented", "skipped", "pending", "unrepresented", "raw", "dated_excluded"),
        0,
    )


# -- helpers / CLI ----------------------------------------------------------


def test_chunked_and_uris() -> None:
    assert rec.chunked(["a", "b", "c"], 0) == [["a", "b", "c"]]
    assert rec.chunked(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    assert rec.key_list_uris("s", "la", 1) == ["s-la.txt"]
    assert rec.key_list_uris("s", "la", 2) == ["s-la-01.txt", "s-la-02.txt"]


def test_write_text_local(tmp_path: Any) -> None:
    path = tmp_path / "keys.txt"
    rec.write_text(None, str(path), "a\n")
    assert path.read_text() == "a\n"


def test_parse_args_validation() -> None:
    with pytest.raises(SystemExit):
        rec.parse_args(["--since", "2026-06-19", "--until", "2026-05-06"])
    with pytest.raises(SystemExit):
        rec.parse_args(["--prefix", "ca"])
    with pytest.raises(SystemExit):
        rec.parse_args(["--reingest"])
    with pytest.raises(SystemExit):
        rec.parse_args(["--record", "--prefix", "ca/"])
    with pytest.raises(SystemExit):
        rec.parse_args(["--record", "--county", "los_angeles"])
    args = rec.parse_args(["--county", "los_angeles", "--county", "san_diego"])
    assert args.county == ["los_angeles", "san_diego"]
    assert args.since is None
    assert args.prefix == ""
    assert args.exclude_prefix == list(rec.DEFAULT_EXCLUDE_PREFIXES)
    assert rec.parse_args(["--exclude-prefix", "x/"]).exclude_prefix == ["x/"]


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda n: None)
    )
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=None))
    assert rec.main([]) == 2


@pytest.mark.parametrize("record", [False, True])
def test_main_runs_with_database(
    monkeypatch: pytest.MonkeyPatch, s3: FakeS3, record: bool, capsys: Any
) -> None:
    db = FakeDb(
        {"derived.documents": [(f"{LA}{_h('a')}.html",)], "RETURNING run_id": [("r",)]}
    )
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda name: s3)
    )
    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(connect=lambda url: db)
    )
    argv = ["--bucket", BUCKET] + (["--record"] if record else [])
    assert rec.main(argv) == 0
    out = capsys.readouterr().out
    assert "RECONCILE_RESULT" in out
    assert bool(db.sql("INSERT INTO telemetry.raw_reconcile_runs")) is record


def test_main_exits_1_when_the_reingest_cannot_launch(
    monkeypatch: pytest.MonkeyPatch, s3: FakeS3, tmp_path: Any
) -> None:
    db = FakeDb({"RETURNING run_id": [("r",)]})
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda name: s3)
    )
    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(connect=lambda url: db)
    )
    argv = [
        "--bucket",
        BUCKET,
        "--record",
        "--reingest",
        "--reingest-script",
        str(tmp_path / "missing.py"),
    ]
    assert rec.main(argv) == 1
