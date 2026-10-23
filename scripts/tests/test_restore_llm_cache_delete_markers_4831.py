"""Tests for restore_llm_cache_delete_markers_4831 (#4831)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import restore_llm_cache_delete_markers_4831 as mod

V = mod.Version
WRITTEN = datetime(2026, 6, 27, 14, 3, 11, tzinfo=UTC)
EXPIRED = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)
KEY = "llm-cache/google-gemini/prompt-abc/doc1.json"


def _entry(key=KEY, written=WRITTEN, marker_at=EXPIRED, storage="STANDARD"):
    return [
        V(key, "v1", False, written, storage),
        V(key, "m1", True, marker_at, is_delete_marker=True),
    ]


def test_restores_lifecycle_expired_entry():
    restores, skipped = mod.select_restorable(_entry())
    assert restores == [mod.Restore(KEY, "m1", "v1")]
    assert not skipped


def test_uses_newest_prior_version():
    entries = _entry() + [V(KEY, "v0", False, datetime(2026, 5, 1, tzinfo=UTC))]
    restores, _ = mod.select_restorable(entries)
    assert restores[0].restored_version_id == "v1"


def test_skips_key_whose_latest_is_a_real_object():
    entries = [V(KEY, "v1", True, WRITTEN)]
    restores, skipped = mod.select_restorable(entries)
    assert restores == [] and not skipped


def test_skips_deliberate_non_midnight_delete():
    restores, skipped = mod.select_restorable(
        _entry(marker_at=datetime(2026, 9, 27, 13, 5, tzinfo=UTC))
    )
    assert restores == []
    assert skipped["not_lifecycle_marker"] == 1


def test_skips_marker_too_soon_after_write():
    restores, skipped = mod.select_restorable(
        _entry(written=datetime(2026, 9, 1, tzinfo=UTC))
    )
    assert restores == []
    assert skipped["too_young_for_ttl"] == 1


def test_skips_glacier_version():
    restores, skipped = mod.select_restorable(_entry(storage="GLACIER"))
    assert restores == []
    assert skipped["unreadable_GLACIER"] == 1


def test_standard_ia_is_restorable():
    restores, _ = mod.select_restorable(_entry(storage="STANDARD_IA"))
    assert len(restores) == 1


def test_marker_without_prior_version():
    entries = [V(KEY, "m1", True, EXPIRED, is_delete_marker=True)]
    restores, skipped = mod.select_restorable(entries)
    assert restores == [] and skipped["no_prior_version"] == 1


def test_remove_markers_batches_and_counts_errors():
    s3 = MagicMock()
    s3.delete_objects.side_effect = [
        {"Errors": [{"Key": "k0", "Code": "AccessDenied", "Message": "x"}]},
        {},
    ]
    restores = [mod.Restore(f"k{i}", f"m{i}", f"v{i}") for i in range(1001)]
    removed, errors = mod.remove_markers(s3, "b", restores)
    assert s3.delete_objects.call_count == 2
    first = s3.delete_objects.call_args_list[0].kwargs["Delete"]["Objects"]
    assert len(first) == 1000 and first[0] == {"Key": "k0", "VersionId": "m0"}
    assert removed == 1000 and len(errors) == 1


def test_list_versions_parses_pages():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Versions": [
                {
                    "Key": KEY,
                    "VersionId": "v1",
                    "IsLatest": False,
                    "LastModified": WRITTEN,
                }
            ],
            "DeleteMarkers": [
                {
                    "Key": KEY,
                    "VersionId": "m1",
                    "IsLatest": True,
                    "LastModified": EXPIRED,
                }
            ],
        }
    ]
    entries = mod.list_versions(s3, "b", "llm-cache/")
    assert entries == _entry()


def test_prompt_dir_label():
    assert mod.prompt_dir("llm-cache/g-m/prompt-x/pages/p.json") == "g-m/prompt-x/pages"
    assert mod.prompt_dir(KEY) == "google-gemini/prompt-abc"


def test_main_dry_run_does_not_delete(monkeypatch, capsys):
    s3 = MagicMock()
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = s3
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(mod, "list_versions", lambda *a: _entry())
    assert mod.main(["--bucket", "b"]) == 0
    s3.delete_objects.assert_not_called()
    assert "restorable keys: 1" in capsys.readouterr().out


def test_main_apply_deletes(monkeypatch):
    s3 = MagicMock()
    s3.delete_objects.return_value = {}
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = s3
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(mod, "list_versions", lambda *a: _entry())
    assert mod.main(["--bucket", "b", "--apply"]) == 0
    s3.delete_objects.assert_called_once()
