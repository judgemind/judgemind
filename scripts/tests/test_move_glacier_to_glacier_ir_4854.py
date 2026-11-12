"""Tests for move_glacier_to_glacier_ir_4854 (#4854)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import move_glacier_to_glacier_ir_4854 as mod

V = mod.Version
T = mod.Target
OLD = datetime(2025, 6, 1, 14, 3, 11, tzinfo=UTC)
NEWER = datetime(2025, 9, 1, 10, 0, 0, tzinfo=UTC)
MIDNIGHT = datetime(2025, 9, 27, 0, 0, 0, tzinfo=UTC)
DAYTIME = datetime(2025, 9, 27, 15, 4, 5, tzinfo=UTC)
CAPTURE = "ca/orange/2025-06-01/doc.pdf"
CACHE = "llm-cache/google-gemini/prompt-abc/doc1.json"


def test_current_glacier_version_is_a_target():
    targets, skipped = mod.select_targets([V(CAPTURE, "v1", True, OLD, "GLACIER", 500)])
    assert targets == [T(CAPTURE, "v1", 500, "current")]
    assert not skipped


def test_readable_storage_classes_are_ignored():
    entries = [
        V(CAPTURE, "v1", True, OLD, "STANDARD_IA"),
        V("ca/x", "v2", True, OLD, "GLACIER_IR"),
        V("ca/y", "v3", True, OLD, "STANDARD"),
    ]
    assert mod.select_targets(entries) == ([], {})


def test_deep_archive_counts_as_cold():
    targets, _ = mod.select_targets([V(CAPTURE, "v1", True, OLD, "DEEP_ARCHIVE")])
    assert [t.reason for t in targets] == ["current"]


def test_noncurrent_behind_newer_version_is_skipped():
    entries = [
        V(CAPTURE, "v1", False, OLD, "GLACIER"),
        V(CAPTURE, "v2", True, NEWER, "STANDARD"),
    ]
    targets, skipped = mod.select_targets(entries)
    assert targets == []
    assert skipped == {("noncurrent_behind_newer_version", "ca/"): 1}


def test_llm_cache_behind_lifecycle_marker_is_a_target():
    entries = [
        V(CACHE, "v1", False, OLD, "GLACIER", 200_000),
        V(CACHE, "m1", True, MIDNIGHT, is_delete_marker=True),
    ]
    targets, skipped = mod.select_targets(entries)
    assert targets == [T(CACHE, "v1", 200_000, "expired_llm_cache")]
    assert not skipped


def test_llm_cache_behind_daytime_marker_is_skipped():
    entries = [
        V(CACHE, "v1", False, OLD, "GLACIER"),
        V(CACHE, "m1", True, DAYTIME, is_delete_marker=True),
    ]
    targets, skipped = mod.select_targets(entries)
    assert targets == []
    assert skipped == {("behind_deliberate_delete_marker", "llm-cache/"): 1}


def test_capture_behind_midnight_marker_is_skipped():
    entries = [
        V(CAPTURE, "v1", False, OLD, "GLACIER"),
        V(CAPTURE, "m1", True, MIDNIGHT, is_delete_marker=True),
    ]
    targets, skipped = mod.select_targets(entries)
    assert targets == []
    assert skipped == {("behind_deliberate_delete_marker", "ca/"): 1}


def test_noncurrent_without_latest_entry_is_skipped():
    targets, skipped = mod.select_targets([V(CAPTURE, "v1", False, OLD, "GLACIER")])
    assert targets == []
    assert skipped == {("noncurrent_no_latest", "ca/"): 1}


def test_already_copied_key_is_not_a_target_again():
    # After --copy: the GLACIER_IR copy is current, the GLACIER source is not.
    entries = [
        V(CACHE, "v1", False, OLD, "GLACIER"),
        V(CACHE, "m1", False, MIDNIGHT, is_delete_marker=True),
        V(CACHE, "v2", True, NEWER.replace(year=2026), "GLACIER_IR"),
    ]
    targets, _ = mod.select_targets(entries)
    assert targets == []


def test_top_prefix():
    assert mod.top_prefix(CAPTURE) == "ca/"
    assert mod.top_prefix("toplevel.json") == "toplevel.json"


def test_summarize_groups_by_prefix_and_reason():
    targets = [
        T(CAPTURE, "v1", 100, "current"),
        T("ca/b", "v2", 50, "current"),
        T(CACHE, "v3", 10, "expired_llm_cache"),
    ]
    assert mod.summarize(targets) == [
        ("ca/", "current", 2, 150),
        ("llm-cache/", "expired_llm_cache", 1, 10),
    ]


def test_estimate_cost_scales_with_tier():
    targets = [T(CAPTURE, f"v{i}", mod.GB, "current") for i in range(1000)]
    bulk = mod.estimate_cost(targets, 14)
    assert bulk["restore_bulk"] == pytest.approx(1000 * 0.0025 + 0.025)
    assert bulk["copy_requests"] == pytest.approx(0.02)
    expedited = mod.estimate_cost(targets, 14, "Expedited")
    assert expedited["restore_expedited"] == pytest.approx(1000 * 0.03 + 10.0)


def _client_error(code: str, headers: dict | None = None) -> ClientError:
    resp = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPHeaders": headers or {}}}
    return ClientError(resp, "Op")


def _s3(restore_headers: dict[str, str | None]):
    s3 = MagicMock()
    s3.exceptions.ClientError = ClientError

    def head(Bucket, Key, VersionId=None):
        if VersionId is None:
            return {"VersionId": "v1"}
        header = restore_headers.get(VersionId)
        return {"Restore": header} if header else {}

    s3.head_object.side_effect = head
    return s3


def test_restore_state():
    s3 = _s3(
        {"a": 'ongoing-request="true"', "b": 'ongoing-request="false", expiry-date="x"'}
    )
    assert mod.restore_state(s3, "bk", T(CAPTURE, "a", 1, "current")) == "in_progress"
    assert mod.restore_state(s3, "bk", T(CAPTURE, "b", 1, "current")) == "done"
    assert mod.restore_state(s3, "bk", T(CAPTURE, "c", 1, "current")) == "none"


def test_request_restores_skips_in_progress_and_counts_errors():
    s3 = _s3({"busy": 'ongoing-request="true"'})
    calls = []

    def restore(**kw):
        calls.append(kw)
        if kw["VersionId"] == "dup":
            raise _client_error("RestoreAlreadyInProgress")
        if kw["VersionId"] == "bad":
            raise _client_error("AccessDenied")

    s3.restore_object.side_effect = restore
    targets = [T(CAPTURE, v, 1, "current") for v in ("new", "busy", "dup", "bad")]
    result = mod.request_restores(s3, "bk", targets, 14, "Bulk")
    assert result == {
        "requested": 1,
        "already_in_progress": 2,
        "error_AccessDenied": 1,
    }
    assert calls[0]["RestoreRequest"] == {
        "Days": 14,
        "GlacierJobParameters": {"Tier": "Bulk"},
    }


def test_copy_restored_copies_only_finished_restores_to_glacier_ir():
    s3 = _s3({"v1": 'ongoing-request="false"', "v2": 'ongoing-request="true"'})
    targets = [T(CAPTURE, "v1", 1, "current"), T("ca/b", "v2", 1, "current")]
    result = mod.copy_restored(s3, "bk", targets)
    assert result == {"copied": 1, "pending_in_progress": 1}
    s3.copy_object.assert_called_once_with(
        Bucket="bk",
        Key=CAPTURE,
        CopySource={"Bucket": "bk", "Key": CAPTURE, "VersionId": "v1"},
        StorageClass="GLACIER_IR",
        MetadataDirective="COPY",
    )


def test_copy_skips_current_key_overwritten_since_listing():
    s3 = _s3({"v0": 'ongoing-request="false"'})
    # head_object without VersionId returns latest "v1", not the target "v0".
    result = mod.copy_restored(s3, "bk", [T(CAPTURE, "v0", 1, "current")])
    assert result == {"skipped_key_changed": 1}
    s3.copy_object.assert_not_called()


def _marker_s3(latest_error: ClientError):
    s3 = _s3({"v1": 'ongoing-request="false"'})

    def head(Bucket, Key, VersionId=None):
        if VersionId is None:
            raise latest_error
        return {"Restore": 'ongoing-request="false"'}

    s3.head_object.side_effect = head
    return s3


def test_copy_restores_expired_llm_cache_entry_behind_marker():
    s3 = _marker_s3(_client_error("404", {"x-amz-delete-marker": "true"}))
    result = mod.copy_restored(s3, "bk", [T(CACHE, "v1", 1, "expired_llm_cache")])
    assert result == {"copied": 1}


def test_copy_skips_expired_entry_when_marker_is_gone():
    s3 = _s3({"v1": 'ongoing-request="false"'})
    # Latest is now a real object (someone rewrote the entry).
    result = mod.copy_restored(s3, "bk", [T(CACHE, "v1", 1, "expired_llm_cache")])
    assert result == {"skipped_key_changed": 1}


def test_copy_skips_current_target_whose_key_was_deleted():
    s3 = _marker_s3(_client_error("404", {"x-amz-delete-marker": "true"}))
    result = mod.copy_restored(s3, "bk", [T(CAPTURE, "v1", 1, "current")])
    assert result == {"skipped_key_changed": 1}


def _page(versions=(), markers=()):
    return {"Versions": list(versions), "DeleteMarkers": list(markers)}


def test_list_versions_parses_versions_and_markers():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        _page(
            versions=[
                {
                    "Key": CACHE,
                    "VersionId": "v1",
                    "IsLatest": False,
                    "LastModified": OLD,
                    "StorageClass": "GLACIER",
                    "Size": 7,
                }
            ],
            markers=[
                {
                    "Key": CACHE,
                    "VersionId": "m1",
                    "IsLatest": True,
                    "LastModified": MIDNIGHT,
                }
            ],
        )
    ]
    out = mod.list_versions(s3, "bk", "llm-cache/")
    assert out == [
        V(CACHE, "v1", False, OLD, "GLACIER", 7),
        V(CACHE, "m1", True, MIDNIGHT, is_delete_marker=True),
    ]


def _run_main(monkeypatch, s3, argv):
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = s3
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return mod.main(argv)


def _listing_s3():
    s3 = _s3({"v1": 'ongoing-request="false"'})
    s3.get_paginator.return_value.paginate.return_value = [
        _page(
            versions=[
                {
                    "Key": CAPTURE,
                    "VersionId": "v1",
                    "IsLatest": True,
                    "LastModified": OLD,
                    "StorageClass": "GLACIER",
                    "Size": 9,
                }
            ]
        )
    ]
    return s3


def test_main_dry_run_makes_no_changes(monkeypatch, capsys):
    s3 = _listing_s3()
    assert _run_main(monkeypatch, s3, ["--bucket", "bk"]) == 0
    s3.restore_object.assert_not_called()
    s3.copy_object.assert_not_called()
    out = capsys.readouterr().out
    assert "targets: 1 versions" in out
    assert "dry run" in out


def test_main_restore_and_copy(monkeypatch, capsys):
    s3 = _listing_s3()
    assert _run_main(monkeypatch, s3, ["--bucket", "bk", "--copy"]) == 0
    s3.copy_object.assert_called_once()
    assert "copied: 1" in capsys.readouterr().out

    s3 = _listing_s3()
    s3.head_object.side_effect = lambda **kw: {}
    assert _run_main(monkeypatch, s3, ["--bucket", "bk", "--restore"]) == 0
    s3.restore_object.assert_called_once()


def test_main_restore_error_exits_nonzero(monkeypatch):
    s3 = _listing_s3()
    s3.head_object.side_effect = lambda **kw: {}
    s3.restore_object.side_effect = _client_error("AccessDenied")
    assert _run_main(monkeypatch, s3, ["--bucket", "bk", "--restore"]) == 1
