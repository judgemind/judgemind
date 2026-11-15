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
#: The shipped deferral list, captured before the autouse fixture empties it.
REAL_DEFERRALS = rec.DEFERRED_BACKLOGS


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


@pytest.fixture(autouse=True)
def _no_default_deferrals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture's federal key is a plain loss unless a test defers it
    (#5036); the real deferral list is tested on its own."""
    monkeypatch.setattr(rec, "DEFERRED_BACKLOGS", ())


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


CC = "ca/contra_costa/superior_court/raw/"


def test_raw_key_document_id_matches_the_scraper_and_reingest_scheme() -> None:
    import uuid

    sha = _h("f")
    assert rec.raw_key_document_id(f"{CC}{sha}.txt") == str(
        uuid.uuid5(uuid.NAMESPACE_URL, sha)
    )
    assert rec.raw_key_document_id(f"{CC}2026/03/04/u.pdf") is None


def test_pre_s3_key_validation_fail_is_matched_by_document_id() -> None:
    """#5023: a CC portal re-capture of a 2025 ruling failed
    ``hearing_date_in_range`` before #4793 made it a flag, and its
    validation row was written before #4773 added ``s3_key``.  The key is
    explained by the row's document id, not lost."""
    key = f"{CC}{_h('c')}.txt"
    other = f"{CC}{_h('d')}.txt"
    doc_id = rec.raw_key_document_id(key)
    assert doc_id
    ex = rec.Explained(validation_fail_documents={doc_id})
    g = timedelta(0)
    assert rec.classify(_raw(key), ex, now=NOW, grace=g) == (
        "skipped",
        "validation_fail_by_document_id",
    )
    assert rec.classify(_raw(other), ex, now=NOW, grace=g) == ("unrepresented", None)
    # a document row still wins
    ex.documents = {key}
    assert rec.classify(_raw(key), ex, now=NOW, grace=g) == ("represented", None)


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
        (501, [], True, False, "over_threshold"),
        (500, [], True, False, "reingest"),
        # the scope was re-ingested in both earlier runs and its keys are back
        (10, ["reingest", "persistent"], True, False, "persistent"),
        (10, [None, "reingest"], True, False, "reingest"),  # healed in between
        (10, ["reingest"], True, False, "reingest"),  # not enough history yet
        # runs that never re-ingested the scope are not a failed heal
        (10, ["reingest_deferred", "reingest"], True, False, "reingest"),
        (10, ["reingest", "over_threshold"], True, False, "reingest"),
        (10, ["report_only", "report_only"], True, False, "reingest"),
        (10, ["reingest", "reingest"], False, False, "report_only"),
        (10, ["reingest", "reingest"], True, True, "reingest_deferred"),
        (501, ["reingest", "reingest"], True, True, "over_threshold"),
    ],
)
def test_decide_scope_action(
    unrep: int,
    previous: list[str | None],
    reingest: bool,
    defer: bool,
    expected: str,
) -> None:
    assert (
        rec.decide_scope_action(
            unrep,
            previous,
            max_auto_reingest=500,
            persist_runs=3,
            reingest=reingest,
            defer=defer,
        )
        == expected
    )


def test_decide_scope_action_without_history_never_persistent() -> None:
    assert (
        rec.decide_scope_action(
            5,
            ["reingest", "reingest"],
            max_auto_reingest=500,
            persist_runs=0,
            reingest=True,
            defer=False,
        )
        == "reingest"
    )


def test_per_scope_threshold_heals_small_scopes_beside_a_big_one() -> None:
    """#5036: one scope over the threshold no longer stops another scope's
    heal; persistence is tracked per scope."""
    big = [_raw(f"{FED}{_h(c)}.txt") for c in "789"]
    small = [_raw(f"{LA}{_h('b')}.html")]
    other = [_raw(f"{SD}{_h('1')}.pdf")]
    actions = rec.decide_scope_actions(
        {"federal/federal": big, "ca/los_angeles": small, "ca/san_diego": other},
        # LA was re-ingested in both earlier runs; SD only in the last one.
        [
            {"ca/los_angeles": "reingest", "ca/san_diego": "reingest"},
            {"ca/los_angeles": "reingest"},
        ],
        max_auto_reingest=2,
        persist_runs=3,
        reingest=True,
        defer=False,
    )
    assert actions == {
        "ca/los_angeles": "persistent",
        "ca/san_diego": "reingest",
        "federal/federal": "over_threshold",
    }


@pytest.mark.parametrize(
    ("scope_actions", "incomplete", "reingest", "defer", "expected"),
    [
        ({}, 0, True, False, "none"),
        ({}, 2, True, False, "reingest"),  # the incomplete batch alone
        ({}, 2, False, False, "report_only"),
        ({}, 2, True, True, "reingest_deferred"),
        ({"a/x": "reingest", "b/y": "over_threshold"}, 0, True, False, "reingest"),
        ({"a/x": "persistent", "b/y": "reingest"}, 0, True, False, "persistent"),
        ({"b/y": "over_threshold"}, 3, True, False, "over_threshold"),
        (
            {"b/y": "over_threshold", "a/x": "report_only"},
            0,
            False,
            False,
            "over_threshold",
        ),
        ({"a/x": "report_only"}, 0, False, False, "report_only"),
        ({"a/x": "reingest_deferred"}, 0, True, True, "reingest_deferred"),
    ],
)
def test_overall_action(
    scope_actions: dict[str, str],
    incomplete: int,
    reingest: bool,
    defer: bool,
    expected: str,
) -> None:
    assert (
        rec.overall_action(
            scope_actions, incomplete=incomplete, reingest=reingest, defer=defer
        )
        == expected
    )


# -- deferred backlogs (#5036) ----------------------------------------------


CL_BACKLOG = rec.DeferredBacklog(
    name="cl",
    issue=3954,
    scopes=("federal/*", "unknown/*", "*/statewide"),
    exclude=("ca/*",),
    captured_before=date(2026, 6, 21),
)


def test_deferred_backlog_matches_scope_patterns_exclusions_and_cutoff() -> None:
    assert CL_BACKLOG.matches(_raw(f"{FED}{_h('7')}.txt"))
    assert CL_BACKLOG.matches(
        _raw(f"south carolina/statewide/courtlistener/raw/{_h('7')}.txt")
    )
    assert CL_BACKLOG.matches(_raw(f"unknown/unknown/courtlistener/raw/{_h('7')}.txt"))
    # The ca/ capture prefix is never deferred, ca/statewide included.
    assert not CL_BACKLOG.matches(_raw(f"{GOV}{_h('5')}.html"))
    assert not CL_BACKLOG.matches(_raw(f"{LA}{_h('b')}.html"))
    # A loss after the cutoff heals as usual.
    assert not CL_BACKLOG.matches(_raw(f"{FED}{_h('7')}.txt", _lm("2026-06-21")))
    assert CL_BACKLOG.matches(_raw(f"{FED}{_h('7')}.txt", _lm("2026-06-20", 23)))
    assert rec.DeferredBacklog("x", 1, ("federal/*",)).matches(
        _raw(f"{FED}{_h('7')}.txt", _lm("2030-01-01"))
    )
    assert CL_BACKLOG.label == "deferred (blocked on #3954)"


def test_default_deferral_is_the_courtlistener_backlog_on_3954() -> None:
    assert [b.issue for b in REAL_DEFERRALS] == [3954]
    (cl,) = REAL_DEFERRALS
    assert cl.matches(_raw(f"{FED}{_h('7')}.txt", _lm("2026-06-18")))
    assert cl.matches(_raw(f"california/statewide/courtlistener/raw/{_h('7')}.txt"))
    assert not cl.matches(_raw(f"{LA}{_h('b')}.html", _lm("2026-06-18")))
    assert not cl.matches(_raw(f"{GOV}{_h('5')}.html"))


def test_deferral_list_is_the_shared_definition() -> None:
    """#5043: the reconciler, reingest_from_s3.py and rebuild_db.py read one
    definition, in the scraper-framework package (oneshot scripts cannot
    import each other)."""
    from ingestion import deferred_backlogs

    assert REAL_DEFERRALS is deferred_backlogs.DEFERRED_BACKLOGS
    assert rec.DeferredBacklog is deferred_backlogs.DeferredBacklog


def test_incomplete_batch_leaves_out_deferred_keys() -> None:
    """#5043: the reconciler never hands the reingest a deferred incomplete
    key; the reingest would skip it, and its retry attempt must not count."""
    fed = _raw(f"{FED}{_h('7')}.txt", _lm("2026-06-01"))
    la = _raw(f"{LA}{_h('8')}.html", _lm("2026-06-02"))
    rep = types.SimpleNamespace(incomplete=[fed, la])
    assert rec.incomplete_batch({"x": rep}, 0, 10, REAL_DEFERRALS) == [la]
    assert rec.incomplete_batch({"x": rep}, 0, 10) == [fed, la]


def test_split_deferred_and_report() -> None:
    fed = [_raw(f"{FED}{_h(c)}.txt") for c in "78"]
    late = _raw(f"{FED}{_h('9')}.txt", _lm("2026-07-01"))
    la = _raw(f"{LA}{_h('b')}.html")
    reports = {
        "federal/federal": rec.ScopeReport(
            "federal/federal", unrepresented=[*fed, late]
        ),
        "ca/los_angeles": rec.ScopeReport("ca/los_angeles", unrepresented=[la]),
    }
    actionable, deferred = rec.split_deferred(reports, (CL_BACKLOG,))
    assert actionable == {"ca/los_angeles": [la], "federal/federal": [late]}
    assert deferred == {"cl": {"federal/federal": 2}}
    assert rec.deferred_report((CL_BACKLOG,), deferred) == [
        {
            "name": "cl",
            "issue": 3954,
            "label": "deferred (blocked on #3954)",
            "keys": 2,
            "scope_count": 1,
            "top_scopes": [["federal/federal", 2]],
        }
    ]
    assert rec.deferred_report((CL_BACKLOG,), {}) == []
    # No backlogs: everything is actionable.
    assert rec.split_deferred(reports, ())[1] == {}


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
    versions = {"hearing_date_in_range": 2}
    db = FakeDb(
        {
            "derived.documents": [("k1",), (None,)],
            "validation_results": [
                ("k2", "doc-2", OLD, "r2", ["hearing_date_in_range"], versions),
                (None, "doc-5", OLD, "r5", None, None),
            ],
            "data_quality_metrics": [("k3",)],
            "ingest_skips": [("k4", "governor_bio_route")],
        }
    )
    ex = rec.fetch_explained(db)
    assert ex == rec.Explained(
        {"k1"},
        {"k4": "governor_bio_route"},
        {"k2"},
        {"k3"},
        {"doc-5"},
        validation_rows={
            "k2": [rec.FailRow(OLD, "r2", ("hearing_date_in_range",), versions)]
        },
        validation_rows_by_document={"doc-5": [rec.FailRow(OLD, "r5")]},
    )
    sql = db.sql("validation_results")
    assert len(sql) == 1
    assert "result = 'fail'" in sql[0][0]
    assert "failed_rules, rule_versions" in sql[0][0]
    assert "non_ruling_pdf_dropped" in db.sql("data_quality_metrics")[0][0]


def test_fetch_explained_before_migration_73_reads_rows_without_versions(
    capsys: Any,
) -> None:
    """#5027: the reconciler may run before the version columns exist."""
    db = FakeDb({"NULL::text[]": [("k2", None, OLD, "r2", None, None)]})
    db.fail_on = "failed_rules, rule_versions"
    ex = rec.fetch_explained(db)
    assert ex.validation_fail == {"k2"}
    assert ex.validation_rows == {"k2": [rec.FailRow(OLD, "r2")]}
    assert db.rollbacks == 1
    assert "rule versions unreadable" in capsys.readouterr().out


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
            # jsonb comes back as a dict (psycopg) or a string; an old run
            # recorded no scope actions.
            "scope_actions": [
                ({"ca/los_angeles": "reingest", "ca/santa_clara": "reingest"},),
                ('{"ca/los_angeles": "persistent"}',),
            ],
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
    # Only LA was re-ingested in both earlier runs.
    assert result["scope_actions"] == {
        "ca/los_angeles": "persistent",
        "ca/santa_clara": "reingest",
        "federal/federal": "reingest",
    }
    assert "SCOPE_ACTION ca/los_angeles action=persistent unrepresented=3" in lines
    assert result["previous_unrepresented"] == [4, 9]
    assert result["previous_actions"] == ["reingest", "persistent"]
    assert db.sql("ORDER BY finished_at")[0][1] == (2,)
    assert db.sql("scope_actions")[0][1] == (2,)
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
    # Not re-ingested, so no later run counts this as a heal attempt.
    assert set(json.loads(update[1])["scope_actions"].values()) == {"reingest_failed"}


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
    assert set(result["scope_actions"].values()) == {"reingest_failed"}
    assert calls == []
    assert s3.puts == {}


def test_over_threshold_files_an_issue_and_does_not_reingest(
    s3: FakeS3, explained: rec.Explained
) -> None:
    db = FakeDb({"RETURNING run_id": [("run-3",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--max-auto-reingest", "1"),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: calls.append(cmd),
        # federal is deferred, so every remaining scope is over 1
        deferrals=(CL_BACKLOG,),
    )
    result = _result(lines)
    assert result["action"] == "over_threshold"
    assert result["scope_actions"] == {
        "ca/los_angeles": "over_threshold",
        "ca/santa_clara": "over_threshold",
    }
    assert result["issue_needed"] is True
    assert calls == []
    assert s3.puts == {}


def test_per_scope_threshold_heals_a_county_beside_a_scope_over_it(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    """#5036: LA (3 keys) is over a threshold of 2, but Santa Clara (2) and
    federal (1) still heal, and the issue is filed for LA."""
    db = FakeDb({"RETURNING run_id": [("run-15",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "2",
            "--reingest-script",
            _stub_script(tmp_path),
        ),
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
    assert result["action"] == "reingest"
    assert result["scope_actions"] == {
        "ca/los_angeles": "over_threshold",
        "ca/santa_clara": "reingest",
        "federal/federal": "reingest",
    }
    assert result["issue_needed"] is True
    assert result["actionable_unrepresented"] == 6
    assert result["deferred"] == 0
    assert [c[c.index("--prefix") + 1] for c in calls] == ["ca/", "federal/"]
    ca_list = s3.puts[calls[0][calls[0].index("--s3-key-list") + 1]]
    assert ca_list.decode().splitlines() == [f"{SC}{_h('3')}.pdf", f"{SC}{_h('4')}.pdf"]
    assert "SCOPE_ACTION ca/los_angeles action=over_threshold unrepresented=3" in lines


def test_deferred_backlog_is_reported_and_never_reingested_or_over_threshold(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    """#5036: a deferred scope far over the threshold neither blocks the
    California heal nor files the issue; it is reported separately."""
    for c in "89fe":
        s3.objects[f"{FED}{_h(c)}.txt"] = (_lm("2026-06-02"), b"")
    db = FakeDb({"RETURNING run_id": [("run-16",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "3",
            "--reingest-script",
            _stub_script(tmp_path),
        ),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
        deferrals=(CL_BACKLOG,),
    )
    result = _result(lines)
    assert result["action"] == "reingest"
    assert result["scope_actions"] == {
        "ca/los_angeles": "reingest",
        "ca/santa_clara": "reingest",
    }
    assert result["issue_needed"] is False
    # Deferred keys stay unrepresented in the counts (and the dashboard).
    assert result["unrepresented"] == 10
    assert result["actionable_unrepresented"] == 5
    assert result["deferred"] == 5
    assert result["deferred_backlogs"] == [
        {
            "name": "cl",
            "issue": 3954,
            "label": "deferred (blocked on #3954)",
            "keys": 5,
            "scope_count": 1,
            "top_scopes": [["federal/federal", 5]],
        }
    ]
    assert result["top_scopes"] == [["ca/los_angeles", 3], ["ca/santa_clara", 2]]
    assert [c[c.index("--prefix") + 1] for c in calls] == ["ca/"]
    assert not any("federal" in uri for uri in s3.puts)
    assert "DEFERRED cl deferred (blocked on #3954) keys=5 scopes=1" in lines
    run_insert = db.sql("INSERT INTO telemetry.raw_reconcile_runs")[0][1]
    assert run_insert[4] == 10  # the run row still counts every unrepresented key
    detail = json.loads(db.sql("UPDATE telemetry.raw_reconcile_runs")[0][1][1])
    assert detail["scope_actions"] == result["scope_actions"]


def test_only_deferred_keys_left_means_nothing_to_do(
    s3: FakeS3, explained: rec.Explained
) -> None:
    """With every non-deferred key healed the run is ``none`` and the
    workflow's healed check (``actionable_unrepresented``) can close the
    issue while the deferred backlog waits."""
    explained.documents.update(f"{LA}{_h(c)}.html" for c in "bcd")
    explained.documents.update(f"{SC}{_h(c)}.pdf" for c in "34")
    db = FakeDb({"RETURNING run_id": [("run-17",)]})
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest"),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: pytest.fail("nothing to re-ingest"),
        deferrals=(CL_BACKLOG,),
    )
    result = _result(lines)
    assert result["action"] == "none"
    assert result["issue_needed"] is False
    assert result["unrepresented"] == 1
    assert result["actionable_unrepresented"] == 0
    assert result["deferred"] == 1
    assert result["scope_actions"] == {}


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
        (
            "represented",
            "skipped",
            "pending",
            "incomplete",
            "incomplete_stuck",
            "unrepresented",
            "raw",
            "dated_excluded",
        ),
        0,
    )


# -- incomplete keys (#5008) ------------------------------------------------


def _incomplete_explained(
    explained: rec.Explained, attempts_b: int = 0, attempts_a: int = 3
) -> rec.Explained:
    """LA ``b`` (unrepresented otherwise) dead-lettered, LA ``a`` (in
    documents) a fallback row at the attempt cap."""
    explained.incomplete = {
        f"{LA}{_h('b')}.html": ("dead_letter", attempts_b),
        f"{LA}{_h('a')}.html": ("single_document_fallback", attempts_a),
    }
    return explained


def _every_scope_over_one(explained: rec.Explained) -> rec.Explained:
    """:func:`_incomplete_explained` with the federal key represented: LA
    (c, d) and Santa Clara (3, 4) both have 2 unrepresented keys, so every
    scope is over a threshold of 1 (#5036)."""
    _incomplete_explained(explained)
    explained.documents.add(f"{FED}{_h('7')}.txt")
    return explained


def test_retry_marker_classify_incomplete_before_represented(
    explained: rec.Explained,
) -> None:
    g = timedelta(hours=6)
    ex = _incomplete_explained(explained)
    assert rec.classify(_raw(f"{LA}{_h('b')}.html"), ex, now=NOW, grace=g) == (
        "incomplete",
        "dead_letter",
    )
    # A fallback document row does not make the key represented.
    assert rec.classify(_raw(f"{LA}{_h('a')}.html"), ex, now=NOW, grace=g) == (
        "incomplete_stuck",
        "single_document_fallback",
    )
    assert rec.classify(
        _raw(f"{LA}{_h('a')}.html"), ex, now=NOW, grace=g, max_incomplete_attempts=4
    ) == ("incomplete", "single_document_fallback")


def test_retry_marker_threshold_is_unrepresented_only() -> None:
    kw = {"reingest": True, "defer": False}
    assert rec.overall_action({}, incomplete=2, **kw) == "reingest"
    # The incomplete batch is sized to the room left, so it never trips the
    # threshold; only unrepresented keys do.
    assert rec.overall_action({"a/x": "reingest"}, incomplete=1, **kw) == "reingest"
    assert rec.overall_action({}, incomplete=0, **kw) == "none"
    # Retrying incomplete keys alone never makes a run persistent: the cap
    # reports them instead (scopes with no unrepresented keys get no action).
    assert (
        rec.decide_scope_actions(
            {},
            [{"a/x": "reingest"}, {"a/x": "reingest"}],
            max_auto_reingest=5,
            persist_runs=3,
            **kw,
        )
        == {}
    )


def test_retry_marker_run_reingests_incomplete_keys_and_counts_attempts(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    ex = _incomplete_explained(explained)
    db = FakeDb(
        {
            "RETURNING run_id": [("run-8",)],
            "stuck_at IS NULL": [(f"{LA}{_h('a')}.html",)],
        }
    )
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        ex,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
    )
    result = _result(lines)
    assert result["action"] == "reingest"
    assert result["incomplete"] == 1
    assert result["incomplete_stuck"] == 1
    assert result["unrepresented"] == 5  # b moved from unrepresented to incomplete
    assert result["incomplete_by_reason"] == {
        "dead_letter": 1,
        "single_document_fallback": 1,
    }
    # The key reached the cap this run: reported once.
    assert result["newly_stuck"] == 1
    assert result["issue_needed"] is True
    assert any(
        line.startswith("INCOMPLETE_STUCK reason=single_document_fallback")
        for line in lines
    )
    # The retryable key is in the reingest list; the stuck one is not.
    uri = calls[0][calls[0].index("--s3-key-list") + 1]
    listed = s3.puts[uri].decode().splitlines()
    assert f"{LA}{_h('b')}.html" in listed
    assert f"{LA}{_h('a')}.html" not in listed
    # One attempt counted for the retryable key, before the reingest.
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert attempts[0][1] == ([f"{LA}{_h('b')}.html"],)
    run_insert = db.sql("INSERT INTO telemetry.raw_reconcile_runs")[0][1]
    assert run_insert[-2:] == (1, 1)
    la = {p[1]: p for _, p in db.sql("INSERT INTO telemetry.raw_reconcile_counts")}[
        "ca/los_angeles"
    ]
    assert la[-2:] == (1, 1)


def test_retry_marker_already_stuck_keys_do_not_refile_the_issue(
    s3: FakeS3, explained: rec.Explained
) -> None:
    ex = _incomplete_explained(explained)
    ex.incomplete.pop(f"{LA}{_h('b')}.html")
    explained.documents.update(f"{LA}{_h(c)}.html" for c in "bcd")
    db = FakeDb({"RETURNING run_id": [("run-9",)]})  # stuck_at already stamped
    lines: list[str] = []
    rec.run(_args("--record"), s3, ex, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["incomplete_stuck"] == 1
    assert result["newly_stuck"] == 0
    assert result["issue_needed"] is False


def test_retry_marker_fetch_explained_reads_incomplete_and_tolerates_missing_table(
    capsys: Any,
) -> None:
    db = FakeDb({"ingest_incomplete": [("k9", "partial_split", 2)]})
    assert rec.fetch_explained(db).incomplete == {"k9": ("partial_split", 2)}
    db = FakeDb()
    db.fail_on = "ingest_incomplete"
    assert rec.fetch_explained(db).incomplete == {}
    assert "ingest_incomplete unreadable" in capsys.readouterr().out


def test_retry_marker_incomplete_batch_fills_the_room_oldest_first() -> None:
    rep = rec.ScopeReport("ca/los_angeles")
    rep.incomplete = [
        _raw(f"{LA}{_h('c')}.html", _lm("2026-06-02")),
        _raw(f"{LA}{_h('b')}.html", _lm("2026-05-13")),
        _raw(f"{LA}{_h('d')}.html", _lm("2026-06-03")),
    ]
    reports = {"ca/los_angeles": rep}
    assert [k.key for k in rec.incomplete_batch(reports, 3, 5)] == [
        f"{LA}{_h('b')}.html",
        f"{LA}{_h('c')}.html",
    ]
    # No room beside the unrepresented keys, which still fit the threshold.
    assert rec.incomplete_batch(reports, 5, 5) == []
    # Over the threshold the unrepresented keys are not re-ingested, so the
    # incomplete keys get the whole threshold.
    assert [k.key for k in rec.incomplete_batch(reports, 7, 2)] == [
        f"{LA}{_h('b')}.html",
        f"{LA}{_h('c')}.html",
    ]


def test_retry_marker_over_threshold_still_drains_incomplete_keys(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    """A lost-key backlog over the threshold files the issue and leaves the
    unrepresented keys alone, but the incomplete batch still re-ingests."""
    _every_scope_over_one(explained)
    db = FakeDb({"RETURNING run_id": [("run-12",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "1",
            "--reingest-script",
            _stub_script(tmp_path),
        ),
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
    assert result["action"] == "over_threshold"
    assert result["issue_needed"] is True
    assert result["reingest_scope"] == "incomplete_only"
    assert result["incomplete_batch"] == 1
    assert [c[c.index("--prefix") + 1] for c in calls] == ["ca/"]
    uri = calls[0][calls[0].index("--s3-key-list") + 1]
    assert s3.puts[uri].decode().splitlines() == [f"{LA}{_h('b')}.html"]
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert attempts[0][1] == ([f"{LA}{_h('b')}.html"],)
    # The run keeps its over_threshold action after the incomplete reingest.
    assert db.sql("UPDATE telemetry.raw_reconcile_runs")[0][1][0] == "over_threshold"


def test_retry_marker_over_threshold_incomplete_only_tolerates_missing_script(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    _every_scope_over_one(explained)
    db = FakeDb({"RETURNING run_id": [("run-13",)]})
    lines: list[str] = []
    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "1",
            "--reingest-script",
            str(tmp_path / "nope.py"),
        ),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: pytest.fail("no reingest without a script"),
    )
    result = _result(lines)
    assert result["action"] == "over_threshold"
    assert "reingest_error" in result


def test_retry_marker_over_threshold_incomplete_only_launch_error_keeps_action(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    _every_scope_over_one(explained)
    db = FakeDb({"RETURNING run_id": [("run-14",)]})
    lines: list[str] = []

    def runner(cmd: list[str], check: bool) -> Any:
        raise OSError("exec format error")

    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "1",
            "--reingest-script",
            _stub_script(tmp_path),
        ),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=runner,
    )
    result = _result(lines)
    assert result["action"] == "over_threshold"
    assert "exec format error" in result["reingest_error"]
    assert db.sql("retry_attempts + 1") == []


def test_retry_marker_incomplete_backlog_never_blocks_the_unrepresented_heal(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    """A large incomplete backlog drains a batch per run instead of making
    the run over_threshold (which would stop the lost-key heal too)."""
    explained.incomplete = {f"{LA}{_h(c)}.html": ("partial_split", 0) for c in "bcd"}
    db = FakeDb({"RETURNING run_id": [("run-10",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args(
            "--record",
            "--reingest",
            "--max-auto-reingest",
            "4",
            "--reingest-script",
            _stub_script(tmp_path),
        ),
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
    # 3 unrepresented (2 SC + 1 federal) leave room for 1 incomplete key.
    assert result["unrepresented"] == 3
    assert result["incomplete"] == 3
    assert result["incomplete_batch"] == 1
    assert result["action"] == "reingest"
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert attempts[0][1] == ([f"{LA}{_h('b')}.html"],)


def test_retry_marker_crashed_reingest_counts_no_attempt(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    _incomplete_explained(explained)
    db = FakeDb({"RETURNING run_id": [("run-11",)]})
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: types.SimpleNamespace(returncode=1),
    )
    assert db.sql("retry_attempts + 1") == []


def test_retry_marker_reset_and_count_helpers() -> None:
    db = FakeDb({"SET retry_attempts = 0": [("k1",), ("k2",)]})
    assert rec.reset_incomplete_attempts(db, 3) == 2
    reset = db.sql("SET retry_attempts = 0")[0]
    assert "retry_attempts >= %s OR stuck_at IS NOT NULL" in reset[0]
    assert reset[1] == (3,)
    rec.count_incomplete_attempts(db, [])
    assert rec.mark_newly_stuck(db, []) == []
    assert not db.sql("retry_attempts + 1")


def test_retry_marker_reset_needs_record() -> None:
    with pytest.raises(SystemExit):
        rec.parse_args(["--reset-incomplete-attempts"])


def test_retry_marker_main_resets_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb({"SET retry_attempts = 0": [("k1",)], "RETURNING run_id": [("run-r",)]})
    fake_psycopg = types.SimpleNamespace(connect=lambda _url: db)
    fake_boto3 = types.SimpleNamespace(client=lambda _name: FakeS3({}))
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert (
        rec.main(["--bucket", BUCKET, "--record", "--reset-incomplete-attempts"]) == 0
    )
    assert db.sql("SET retry_attempts = 0")


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


# -- issue text and scope history (#5036) ------------------------------------


def _deferred_run_result(s3: FakeS3, explained: rec.Explained) -> dict[str, Any]:
    for c in "89":
        s3.objects[f"{FED}{_h(c)}.txt"] = (_lm("2026-06-02"), b"")
    lines: list[str] = []
    rec.run(
        _args("--max-auto-reingest", "2"),
        s3,
        explained,
        out=lines.append,
        now=NOW,
        deferrals=(CL_BACKLOG,),
    )
    return _result(lines)


def test_issue_summary_lists_actions_per_scope_and_deferred_backlogs(
    s3: FakeS3, explained: rec.Explained
) -> None:
    text = rec.issue_summary(_deferred_run_result(s3, explained))
    assert "**Action:** `over_threshold`" in text
    assert (
        "**Unrepresented:** 5 to act on, of 14 raw keys (8 including deferred"
    ) in text
    assert (
        "**Deferred (blocked on #3954):** 3 keys in 1 scopes, not re-ingested and "
        "not counted toward the threshold (largest: federal/federal 3)"
    ) in text
    assert "Threshold: 2 keys per scope." in text
    assert "| Scope | Unrepresented | Action |" in text
    # Largest scope first; deferred scopes are not in the action table.
    assert text.index("| ca/los_angeles | 3 | `over_threshold` |") < text.index(
        "| ca/santa_clara | 2 | `report_only` |"
    )
    assert "| federal/federal |" not in text
    assert "Newly stuck" not in text


def test_issue_summary_with_nothing_to_act_on_and_newly_stuck_keys() -> None:
    text = rec.issue_summary(
        {
            "action": "none",
            "unrepresented": 0,
            "raw": 9,
            "newly_stuck": 1,
            "newly_stuck_sample": ["k1"],
            "max_auto_reingest": 500,
        }
    )
    assert "**Unrepresented:** 0 to act on, of 9 raw keys" in text
    assert "Newly stuck (sample): k1" in text
    assert "No scope has unrepresented keys to act on." in text
    assert "Deferred" not in text


def test_main_issue_summary_needs_no_database(
    s3: FakeS3,
    explained: rec.Explained,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_deferred_run_result(s3, explained)))
    capsys.readouterr()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert rec.main(["--issue-summary", str(path)]) == 0
    assert "Deferred (blocked on #3954)" in capsys.readouterr().out


def test_fetch_recent_scope_actions_shapes() -> None:
    assert rec.fetch_recent_scope_actions(FakeDb(), 0) == []
    db = FakeDb({"scope_actions": [({"ca/x": "reingest"},), (None,), ("[]",)]})
    assert rec.fetch_recent_scope_actions(db, 3) == [{"ca/x": "reingest"}, {}, {}]
    assert db.sql("scope_actions")[0][1] == (3,)
