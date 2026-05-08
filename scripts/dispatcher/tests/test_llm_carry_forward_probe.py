"""Unit tests for scripts/dispatcher/llm_carry_forward_probe.py (issue #4309).

Fixture-driven — no live database. Each test feeds a synthetic ``run_audit``
summary into :func:`evaluate` and asserts the threshold logic produces the
expected findings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
# parents[2] from scripts/dispatcher/tests/ is scripts/.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.llm_carry_forward_probe import (  # noqa: E402
    DEFAULT_CLUSTER_THRESHOLD,
    DEFAULT_RIVERSIDE_LEGACY_CAP,
    Finding,
    _load_state,
    _load_state_from_db,
    _percent_jump,
    _save_state,
    _save_state_to_db,
    evaluate,
    extract_county_totals,
    render_summary_comment,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic ``run_audit`` summaries
# ---------------------------------------------------------------------------


def _county_bucket(
    *,
    rulings: int = 100,
    outcome_continue: int = 0,
    motion_type_contradiction: int = 0,
    case_title_text_mismatch: int = 0,
    cluster: int = 0,
    examples: list | None = None,
) -> dict:
    """Build one county dict matching the run_audit shape."""
    ex = examples or []
    return {
        "total_rulings": rulings,
        "outcome_continue": {"count": outcome_continue, "examples": ex},
        "motion_type_contradiction": {
            "count": motion_type_contradiction,
            "examples": ex,
        },
        "case_title_text_mismatch": {
            "count": case_title_text_mismatch,
            "examples": ex,
        },
        "all_same_case_title_cluster": {"count": cluster, "examples": ex},
    }


def _summary(counties: dict[str, dict]) -> dict:
    """Wrap a counties dict in the run_audit envelope."""
    return {
        "filter": {"county": None, "since": None},
        "counties": counties,
        "totals": {
            "rulings_audited": sum(c["total_rulings"] for c in counties.values()),
            "outcome_continue": sum(
                c["outcome_continue"]["count"] for c in counties.values()
            ),
            "motion_type_contradiction": sum(
                c["motion_type_contradiction"]["count"] for c in counties.values()
            ),
            "case_title_text_mismatch": sum(
                c["case_title_text_mismatch"]["count"] for c in counties.values()
            ),
            "all_same_case_title_cluster": sum(
                c["all_same_case_title_cluster"]["count"] for c in counties.values()
            ),
        },
        "all_clean": all(
            c["outcome_continue"]["count"] == 0
            and c["motion_type_contradiction"]["count"] == 0
            and c["case_title_text_mismatch"]["count"] == 0
            and c["all_same_case_title_cluster"]["count"] == 0
            for c in counties.values()
        ),
    }


# ---------------------------------------------------------------------------
# outcome_continue — any non-zero is a flag
# ---------------------------------------------------------------------------


def test_outcome_continue_any_nonzero_flags() -> None:
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    assert len(findings) == 1
    f = findings[0]
    assert f.probe == "outcome_continue"
    assert f.county == "Ventura"
    assert f.should_file_issue is True
    assert "outcome_continue" in f.title
    assert "Ventura" in f.title


def test_outcome_continue_zero_does_not_flag() -> None:
    summary = _summary({"Ventura": _county_bucket(outcome_continue=0)})
    findings = evaluate(summary, prior_totals=None)
    # No findings at all when every axis is zero.
    assert findings == []


def test_outcome_continue_per_county_isolation() -> None:
    """Each county trips its own finding."""
    summary = _summary(
        {
            "Ventura": _county_bucket(outcome_continue=1),
            "Santa Clara": _county_bucket(outcome_continue=2),
            "Orange": _county_bucket(outcome_continue=0),
        }
    )
    findings = [
        f for f in evaluate(summary, prior_totals=None) if f.probe == "outcome_continue"
    ]
    assert {f.county for f in findings} == {"Ventura", "Santa Clara"}


# ---------------------------------------------------------------------------
# all_same_case_title_cluster — non-Riverside threshold (default 5)
# ---------------------------------------------------------------------------


def test_cluster_below_default_threshold_does_not_flag() -> None:
    summary = _summary({"San Diego": _county_bucket(cluster=DEFAULT_CLUSTER_THRESHOLD)})
    findings = evaluate(summary, prior_totals=None)
    # exactly == cap is not a trip; only > cap fires.
    assert findings == []


def test_cluster_above_default_threshold_flags() -> None:
    summary = _summary(
        {"San Diego": _county_bucket(cluster=DEFAULT_CLUSTER_THRESHOLD + 1)}
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    assert len(cluster_findings) == 1
    f = cluster_findings[0]
    assert f.county == "San Diego"
    assert f.should_file_issue is True
    assert f.details["cap"] == DEFAULT_CLUSTER_THRESHOLD


# ---------------------------------------------------------------------------
# all_same_case_title_cluster — Riverside legacy cap
# ---------------------------------------------------------------------------


def test_cluster_riverside_below_legacy_cap_does_not_flag() -> None:
    """Riverside has a documented backlog of 167 as of 2026-05-06; the legacy
    cap (default 80) lets that history pass while still catching new growth.
    """
    summary = _summary(
        {"Riverside": _county_bucket(cluster=DEFAULT_RIVERSIDE_LEGACY_CAP)}
    )
    findings = evaluate(summary, prior_totals=None)
    # exactly == legacy cap is not a trip.
    assert findings == []


def test_cluster_riverside_above_legacy_cap_flags() -> None:
    summary = _summary(
        {"Riverside": _county_bucket(cluster=DEFAULT_RIVERSIDE_LEGACY_CAP + 1)}
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    assert len(cluster_findings) == 1
    assert cluster_findings[0].details["cap"] == DEFAULT_RIVERSIDE_LEGACY_CAP


def test_cluster_riverside_uses_separate_cap_from_others() -> None:
    """Riverside at 100 should pass (under legacy cap 80? no — over). Pick a
    value that is over the default cluster_threshold (5) but under the
    Riverside legacy cap (80)."""
    summary = _summary(
        {
            "Riverside": _county_bucket(cluster=50),
            "Ventura": _county_bucket(cluster=50),
        }
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_counties = {
        f.county for f in findings if f.probe == "all_same_case_title_cluster"
    }
    # Riverside (under legacy cap 80) does NOT flag; Ventura (over cap 5) does.
    assert "Ventura" in cluster_counties
    assert "Riverside" not in cluster_counties


def test_cluster_riverside_case_insensitive() -> None:
    """Riverside detector handles different casing variants."""
    summary = _summary(
        {"riverside": _county_bucket(cluster=10)},  # lowercase
    )
    findings = evaluate(summary, prior_totals=None)
    cluster_findings = [f for f in findings if f.probe == "all_same_case_title_cluster"]
    # 10 is below the Riverside legacy cap (80), so no flag — confirms the
    # detector matched riverside as Riverside, not as a non-Riverside county
    # (which would have flagged at threshold 5).
    assert cluster_findings == []


# ---------------------------------------------------------------------------
# motion_type_contradiction / case_title_text_mismatch — % jump only
# ---------------------------------------------------------------------------


def test_noisy_axes_silent_on_first_run() -> None:
    """No prior_totals means first-run mode — noisy axes never fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=999)})
    findings = evaluate(summary, prior_totals=None)
    # No noisy-axis fires; outcome_continue and cluster are zero.
    assert findings == []


def test_noisy_axes_jump_above_threshold_flags() -> None:
    """+30% jump on a noisy axis (over the +25% default) should fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=130)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1
    f = jump_findings[0]
    assert f.probe == "jump_motion_type_contradiction"
    assert f.details["prior_count"] == 100
    assert f.details["current_count"] == 130
    assert 0.29 < f.details["jump_fraction"] < 0.31


def test_noisy_axes_jump_below_threshold_silent() -> None:
    """+10% jump (under +25%) should not fire."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=110)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert jump_findings == []


def test_noisy_axes_decrease_silent() -> None:
    """A drop in count never fires."""
    summary = _summary({"Ventura": _county_bucket(case_title_text_mismatch=50)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"case_title_text_mismatch": 100}},
    )
    assert all(not f.probe.startswith("jump_") for f in findings)


def test_noisy_axes_zero_to_nonzero_below_floor_silent() -> None:
    """Prior=0, current=2 must not fire (below 5-floor)."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=2)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 0}},
    )
    assert all(not f.probe.startswith("jump_") for f in findings)


def test_noisy_axes_zero_to_floor_fires() -> None:
    """Prior=0, current=5 fires (crosses the 5-floor)."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=5)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 0}},
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1


def test_noisy_axes_threshold_override() -> None:
    """Custom --noisy-jump-fraction is honored."""
    summary = _summary({"Ventura": _county_bucket(motion_type_contradiction=110)})
    findings = evaluate(
        summary,
        prior_totals={"Ventura": {"motion_type_contradiction": 100}},
        noisy_jump_fraction=0.05,  # 5% → 10% jump fires
    )
    jump_findings = [f for f in findings if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1


# ---------------------------------------------------------------------------
# _percent_jump
# ---------------------------------------------------------------------------


def test_percent_jump_normal() -> None:
    assert _percent_jump(100, 130) == 0.30


def test_percent_jump_zero_prior_below_floor() -> None:
    assert _percent_jump(0, 4) is None


def test_percent_jump_zero_prior_at_floor() -> None:
    assert _percent_jump(0, 5) == float("inf")


def test_percent_jump_no_change() -> None:
    assert _percent_jump(50, 50) is None


def test_percent_jump_decrease() -> None:
    assert _percent_jump(100, 50) is None


# ---------------------------------------------------------------------------
# extract_county_totals + state round-trip
# ---------------------------------------------------------------------------


def test_extract_county_totals_shape() -> None:
    summary = _summary(
        {
            "Ventura": _county_bucket(outcome_continue=2, cluster=3),
            "San Diego": _county_bucket(case_title_text_mismatch=10),
        }
    )
    totals = extract_county_totals(summary)
    assert totals["Ventura"]["outcome_continue"] == 2
    assert totals["Ventura"]["all_same_case_title_cluster"] == 3
    assert totals["Ventura"]["motion_type_contradiction"] == 0
    assert totals["San Diego"]["case_title_text_mismatch"] == 10


def test_state_roundtrip(tmp_path) -> None:
    """_save_state writes JSON readable by _load_state."""
    path = str(tmp_path / "state.json")
    totals = {"Ventura": {"outcome_continue": 1, "motion_type_contradiction": 7}}
    _save_state(path, totals)
    loaded = _load_state(path)
    assert loaded == totals


def test_load_state_missing_returns_none(tmp_path) -> None:
    """Missing state file is first-run mode (returns None)."""
    path = str(tmp_path / "absent.json")
    assert _load_state(path) is None


def test_load_state_malformed_returns_none(tmp_path) -> None:
    """Malformed JSON is treated as first-run."""
    path = tmp_path / "bad.json"
    path.write_text("not json{", encoding="utf-8")
    assert _load_state(str(path)) is None


def test_load_state_none_path_returns_none() -> None:
    """Explicit None path is first-run mode."""
    assert _load_state(None) is None


# ---------------------------------------------------------------------------
# render_summary_comment
# ---------------------------------------------------------------------------


def test_render_summary_comment_clean_run() -> None:
    """Clean run renders a heartbeat with totals and 'all clean' line."""
    summary = _summary(
        {
            "Ventura": _county_bucket(rulings=200),
            "Riverside": _county_bucket(rulings=400),
        }
    )
    text = render_summary_comment(summary, [])
    assert "Weekly LLM carry-forward audit" in text
    assert "All thresholds clean" in text
    assert "Ventura" in text
    assert "Riverside" in text


def test_render_summary_comment_with_findings() -> None:
    """Comment with findings names the trips and counts them."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=2)})
    findings = evaluate(summary, prior_totals=None)
    text = render_summary_comment(summary, findings)
    assert "1 threshold trip" in text or "1 threshold trips" in text
    assert "outcome_continue" in text


# ---------------------------------------------------------------------------
# Finding shape — title + body invariants for SKILL.md consumption
# ---------------------------------------------------------------------------


def test_finding_title_starts_with_bracketed_prefix() -> None:
    """SKILL.md dedups by bracketed title prefix; titles must start with `[`."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    for f in findings:
        assert f.title.startswith("[llm-carry-forward "), (
            f"finding title must start with '[llm-carry-forward ' for SKILL.md "
            f"dedup search; got {f.title!r}"
        )


def test_finding_body_includes_county_axis_repro() -> None:
    """Bodies should self-describe enough that an agent can act on them."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    f = findings[0]
    assert "Ventura" in f.body
    assert "outcome_continue" in f.body
    # Repro stanza references the audit script and county filter.
    assert "audit_llm_carry_forward.py" in f.body
    assert "--county Ventura" in f.body


def test_findings_serializable_to_json() -> None:
    """The probe writes findings via dataclasses.asdict + json.dumps; ensure
    everything in the Finding round-trips cleanly."""
    summary = _summary(
        {
            "Ventura": _county_bucket(
                outcome_continue=1, cluster=10, examples=[{"k": "v"}]
            )
        }
    )
    findings = evaluate(summary, prior_totals=None)
    from dataclasses import asdict

    payload = json.dumps([asdict(f) for f in findings], default=str)
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert all(isinstance(item, dict) for item in parsed)
    assert all("title" in item and "body" in item for item in parsed)


def test_evaluate_returns_finding_instances() -> None:
    """Type guard: evaluate returns Finding objects, not dicts."""
    summary = _summary({"Ventura": _county_bucket(outcome_continue=1)})
    findings = evaluate(summary, prior_totals=None)
    assert all(isinstance(f, Finding) for f in findings)


# ---------------------------------------------------------------------------
# DB-backed state — issue #4318
# ---------------------------------------------------------------------------
#
# The probe persists per-county totals to
# ``dispatcher.scheduled_skills.last_run_state`` so a fresh ECS task can
# read prior totals on the next fire (the local --state file is wiped
# between fires). These tests use a fake psycopg module that records
# every executed statement and lets us script the SELECT result.


class _FakeCursor:
    """Records execute() calls; replays a queued result on fetchone()."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._fetch_queue: list = []
        self.rowcount = 0

    def queue_fetchone(self, value) -> None:
        """Queue the value the next fetchone() returns."""
        self._fetch_queue.append(value)

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        if not self._fetch_queue:
            return None
        return self._fetch_queue.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self, cur: _FakeCursor) -> None:
        self._cur = cur
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cur

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class _FakePsycopg:
    """Stand-in for the ``psycopg`` module imported lazily by the probe."""

    def __init__(self, conn_factory) -> None:
        self._conn_factory = conn_factory
        self.connect_calls: list[str] = []

    def connect(self, dsn: str) -> _FakeConn:
        self.connect_calls.append(dsn)
        return self._conn_factory()


def _install_fake_psycopg(monkeypatch, fake) -> None:
    """Make ``import psycopg`` inside the probe return ``fake``."""
    monkeypatch.setitem(sys.modules, "psycopg", fake)


# ---------------------------------------------------------------------------
# _load_state_from_db
# ---------------------------------------------------------------------------


def test_load_state_from_db_returns_dict_when_row_present(monkeypatch) -> None:
    """Happy path — DB row exists with a JSONB dict."""
    cur = _FakeCursor()
    cur.queue_fetchone(({"Ventura": {"motion_type_contradiction": 100}},))
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    result = _load_state_from_db("audit-llm-carry-forward", "postgres://test")
    assert result == {"Ventura": {"motion_type_contradiction": 100}}
    # Connected once with the supplied DSN.
    assert fake.connect_calls == ["postgres://test"]
    # Executed exactly one parameterised SELECT against scheduled_skills.
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "SELECT last_run_state" in sql
    assert "FROM dispatcher.scheduled_skills" in sql
    assert "WHERE name = %s" in sql
    assert params == ("audit-llm-carry-forward",)


def test_load_state_from_db_returns_none_when_skill_name_empty() -> None:
    """Empty skill_name short-circuits before opening a connection."""
    assert _load_state_from_db("", "postgres://test") is None
    assert _load_state_from_db(None, "postgres://test") is None


def test_load_state_from_db_returns_none_when_dsn_empty() -> None:
    """Empty dsn short-circuits before opening a connection."""
    assert _load_state_from_db("audit-llm-carry-forward", "") is None


def test_load_state_from_db_returns_none_when_row_missing(monkeypatch) -> None:
    """Skill row does not exist (fetchone returns None)."""
    cur = _FakeCursor()
    cur.queue_fetchone(None)
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    assert _load_state_from_db("audit-llm-carry-forward", "postgres://test") is None


def test_load_state_from_db_returns_none_when_value_null(monkeypatch) -> None:
    """Skill row exists but last_run_state is NULL (first-ever run)."""
    cur = _FakeCursor()
    cur.queue_fetchone((None,))
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    assert _load_state_from_db("audit-llm-carry-forward", "postgres://test") is None


def test_load_state_from_db_returns_none_when_value_not_dict(monkeypatch) -> None:
    """Defensive: a row whose JSONB is not an object (e.g. a list) is ignored."""
    cur = _FakeCursor()
    cur.queue_fetchone((["not", "a", "dict"],))
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    assert _load_state_from_db("audit-llm-carry-forward", "postgres://test") is None


def test_load_state_from_db_returns_none_when_psycopg_missing(monkeypatch) -> None:
    """psycopg unavailable — the helper returns None instead of raising."""
    # Make ``import psycopg`` fail.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert _load_state_from_db("audit-llm-carry-forward", "postgres://test") is None


def test_load_state_from_db_returns_none_on_db_error(monkeypatch) -> None:
    """A psycopg.connect error is logged and converted to None."""

    class _BoomPsycopg:
        def connect(self, dsn: str):
            raise RuntimeError("connection refused")

    _install_fake_psycopg(monkeypatch, _BoomPsycopg())
    assert _load_state_from_db("audit-llm-carry-forward", "postgres://test") is None


# ---------------------------------------------------------------------------
# _save_state_to_db
# ---------------------------------------------------------------------------


def test_save_state_to_db_runs_update_and_commits(monkeypatch) -> None:
    """Happy path — UPDATE runs against the scheduled_skills row + commit fires."""
    cur = _FakeCursor()
    cur.rowcount = 1  # 1 row updated
    fake_conn = _FakeConn(cur)
    fake = _FakePsycopg(lambda: fake_conn)
    _install_fake_psycopg(monkeypatch, fake)

    totals = {"Ventura": {"motion_type_contradiction": 100}}
    ok = _save_state_to_db("audit-llm-carry-forward", "postgres://test", totals)
    assert ok is True
    assert fake_conn.committed is True
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "UPDATE dispatcher.scheduled_skills" in sql
    assert "SET last_run_state" in sql
    assert "WHERE name = %s" in sql
    # Payload is JSON-serialised + parameterised.
    payload, name = params
    assert name == "audit-llm-carry-forward"
    assert json.loads(payload) == totals


def test_save_state_to_db_returns_false_when_no_row(monkeypatch) -> None:
    """UPDATE found no row — the row is missing for this skill name."""
    cur = _FakeCursor()
    cur.rowcount = 0
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    totals = {"Ventura": {"outcome_continue": 1}}
    ok = _save_state_to_db("typo-skill-name", "postgres://test", totals)
    assert ok is False


def test_save_state_to_db_returns_false_when_skill_name_empty() -> None:
    """Empty skill_name short-circuits."""
    assert _save_state_to_db("", "postgres://test", {}) is False
    assert _save_state_to_db(None, "postgres://test", {}) is False


def test_save_state_to_db_returns_false_when_dsn_empty() -> None:
    """Empty dsn short-circuits."""
    assert _save_state_to_db("audit-llm-carry-forward", "", {}) is False


def test_save_state_to_db_returns_false_when_psycopg_missing(monkeypatch) -> None:
    """psycopg unavailable — the helper returns False instead of raising."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert _save_state_to_db("audit-llm-carry-forward", "postgres://test", {}) is False


def test_save_state_to_db_returns_false_on_db_error(monkeypatch) -> None:
    """A connect-time error is logged and the helper returns False."""

    class _BoomPsycopg:
        def connect(self, dsn: str):
            raise RuntimeError("disk full")

    _install_fake_psycopg(monkeypatch, _BoomPsycopg())
    assert _save_state_to_db("audit-llm-carry-forward", "postgres://test", {}) is False


# ---------------------------------------------------------------------------
# DB-backed state — round-trip + integration with evaluate()
# ---------------------------------------------------------------------------


def test_db_state_roundtrip_drives_jump_detection_on_second_run(monkeypatch) -> None:
    """Two consecutive probe-style runs: the second sees the first's totals
    via the DB-backed state and produces a +30% jump finding.

    Acceptance criterion 2 of issue #4318: after two consecutive scheduled
    fires of /audit-llm-carry-forward, the second fire's findings include
    at least one ``jump_*`` finding when synthetic data has a > 25% delta
    on a noisy axis.
    """
    # Single shared "DB" — the same row persists between save and load.
    storage: dict[str, dict] = {}

    class _StorageCursor:
        def __init__(self) -> None:
            self.rowcount = 0
            self._fetch_queue: list = []

        def execute(self, sql: str, params: tuple = ()) -> None:
            if "UPDATE" in sql:
                payload_json, name = params
                storage[name] = json.loads(payload_json)
                self.rowcount = 1
            elif "SELECT last_run_state" in sql:
                (name,) = params
                row_value = storage.get(name)
                self._fetch_queue.append(
                    (row_value,) if row_value is not None else (None,)
                )

        def fetchone(self):
            if not self._fetch_queue:
                return None
            return self._fetch_queue.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _StorageConn:
        def cursor(self) -> _StorageCursor:
            return _StorageCursor()

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    class _StoragePsycopg:
        def connect(self, dsn: str) -> _StorageConn:
            return _StorageConn()

    _install_fake_psycopg(monkeypatch, _StoragePsycopg())

    skill = "audit-llm-carry-forward"
    dsn = "postgres://test"

    # --- Run 1: prior state is empty; persist totals=100 on the noisy axis.
    summary_run1 = _summary({"Ventura": _county_bucket(motion_type_contradiction=100)})
    prior_run1 = _load_state_from_db(skill, dsn)
    assert prior_run1 is None  # first run sees no baseline

    findings_run1 = evaluate(summary_run1, prior_totals=prior_run1)
    # No jump finding on first run (no baseline).
    assert all(not f.probe.startswith("jump_") for f in findings_run1)

    saved = _save_state_to_db(skill, dsn, extract_county_totals(summary_run1))
    assert saved is True
    assert storage[skill]["Ventura"]["motion_type_contradiction"] == 100

    # --- Run 2: load prior baseline + new totals=130 (+30% > +25% threshold).
    summary_run2 = _summary({"Ventura": _county_bucket(motion_type_contradiction=130)})
    prior_run2 = _load_state_from_db(skill, dsn)
    assert prior_run2 is not None
    assert prior_run2["Ventura"]["motion_type_contradiction"] == 100

    findings_run2 = evaluate(summary_run2, prior_totals=prior_run2)
    jump_findings = [f for f in findings_run2 if f.probe.startswith("jump_")]
    assert len(jump_findings) == 1
    assert jump_findings[0].probe == "jump_motion_type_contradiction"
    assert jump_findings[0].details["prior_count"] == 100
    assert jump_findings[0].details["current_count"] == 130


def test_load_state_from_db_falls_back_to_file_when_db_returns_none(
    monkeypatch, tmp_path
) -> None:
    """Acceptance criterion 3: missing DB state falls back to the local
    --state file path (development fallback).

    This test asserts the helper composition the probe uses in main():
    ``prior = _load_state_from_db(...) or _load_state(--state)``.
    Both layers missing => first-run mode (None).
    """
    # Make the DB-backed read return None (simulates the column not yet
    # populated in dev or no row).
    cur = _FakeCursor()
    cur.queue_fetchone(None)
    fake = _FakePsycopg(lambda: _FakeConn(cur))
    _install_fake_psycopg(monkeypatch, fake)

    state_path = tmp_path / "last_totals.json"
    fallback_totals = {"Ventura": {"motion_type_contradiction": 50}}
    _save_state(str(state_path), fallback_totals)

    # Compose the same way main() does.
    prior = _load_state_from_db("audit-llm-carry-forward", "postgres://test")
    if prior is None:
        prior = _load_state(str(state_path))
    assert prior == fallback_totals
