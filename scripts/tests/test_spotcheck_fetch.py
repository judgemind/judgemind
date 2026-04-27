"""Tests for scripts/spotcheck/fetch.py."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spotcheck"))

from fetch import (
    STRATEGIES,
    ExpansionStrategy,
    OriginalsStrategy,
    RulingsStrategy,
    _safe_dirname,
    fetch_manifest,
    register_strategy,
)


class TestOriginalsStrategyRecursiveChain:
    """OriginalsStrategy query must use WITH RECURSIVE to walk multi-hop chains (#3537)."""

    def _get_derived_sql(self, tmp_path: Path) -> str:
        """Capture the SQL passed to _run_db_query by OriginalsStrategy.expand."""
        captured: list[str] = []

        def capture_sql(sql: str, params: Any = None) -> list[dict[str, Any]]:
            captured.append(sql)
            return []

        item_dir = tmp_path / "test-doc"
        item_dir.mkdir()
        strategy = OriginalsStrategy()
        with (
            patch("fetch._fetch_s3_object", return_value=False),
            patch("fetch._run_db_query", side_effect=capture_sql),
        ):
            strategy.expand("ca/contra_costa/doc_a.pdf", item_dir)

        assert captured, "Expected _run_db_query to be called at least once"
        return captured[0]

    def test_query_uses_recursive_cte(self, tmp_path: Path) -> None:
        sql = self._get_derived_sql(tmp_path)
        assert "WITH RECURSIVE" in sql, (
            "OriginalsStrategy must use WITH RECURSIVE to walk multi-hop "
            "previous_version_id chains (#3537)."
        )

    def test_query_recursion_joins_previous_version_id(self, tmp_path: Path) -> None:
        sql = self._get_derived_sql(tmp_path)
        normalized = re.sub(r"\s+", " ", sql).lower()
        assert "d.id = td.previous_version_id" in normalized, (
            "Recursive arm must join target_docs td ON d.id = td.previous_version_id "
            "to walk each hop of the supersede chain."
        )


class TestStrategyRegistry:
    """Tests for the expansion strategy registry."""

    def test_rulings_registered(self) -> None:
        assert "rulings" in STRATEGIES

    def test_originals_registered(self) -> None:
        assert "originals" in STRATEGIES

    def test_rulings_strategy_is_correct_class(self) -> None:
        assert STRATEGIES["rulings"] is RulingsStrategy

    def test_originals_strategy_is_correct_class(self) -> None:
        assert STRATEGIES["originals"] is OriginalsStrategy

    def test_register_new_strategy(self) -> None:
        """Verify the register_strategy decorator works for new types."""

        @register_strategy("test_entity")
        class TestStrategy(ExpansionStrategy):
            def expand(self, entity_id: str, item_dir: Path) -> dict[str, Any]:
                return {"id": entity_id}

        assert "test_entity" in STRATEGIES
        assert STRATEGIES["test_entity"] is TestStrategy

        # Cleanup
        del STRATEGIES["test_entity"]


class TestSafeDirname:
    """Tests for the _safe_dirname helper."""

    def test_uuid_unchanged(self) -> None:
        uuid = "f51849ca-1234-5678-9abc-def012345678"
        assert _safe_dirname(uuid) == uuid

    def test_s3_key_sanitized(self) -> None:
        key = "CA/Los Angeles/dept-1/doc.pdf"
        result = _safe_dirname(key)
        assert "/" not in result
        assert result == "CA_Los_Angeles_dept-1_doc.pdf"

    def test_spaces_replaced(self) -> None:
        key = "CA/Los Angeles/some doc.pdf"
        result = _safe_dirname(key)
        assert " " not in result


class TestRulingsStrategy:
    """Tests for the rulings expansion strategy."""

    def test_expand_creates_db_record(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        item_dir.mkdir()

        db_response = [
            {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "ruling_text": "The motion is GRANTED.",
                "outcome": "granted",
                "s3_key": "CA/LA/doc.pdf",
                "s3_bucket": "judgemind-documents-dev",
                "doc_format": "pdf",
                "case_id": "test-case-id",
            }
        ]

        strategy = RulingsStrategy()
        with (
            patch("fetch._run_db_query", return_value=db_response),
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("a1b2c3d4-e5f6-7890-abcd-ef1234567890", item_dir)

        assert "db_record.json" in result["artifacts"]
        assert (item_dir / "db_record.json").exists()

    def test_expand_fetches_s3_doc(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        item_dir.mkdir()

        db_response = [
            {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "s3_key": "CA/LA/doc.pdf",
                "s3_bucket": "judgemind-documents-dev",
                "doc_format": "pdf",
                "case_id": "test-case-id",
            }
        ]

        strategy = RulingsStrategy()
        with (
            patch("fetch._run_db_query", return_value=db_response),
            patch("fetch._fetch_s3_object", return_value=True) as mock_s3,
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("a1b2c3d4-e5f6-7890-abcd-ef1234567890", item_dir)

        assert "original.pdf" in result["artifacts"]
        mock_s3.assert_called_once()
        call_args = mock_s3.call_args
        assert call_args[0][0] == "CA/LA/doc.pdf"
        assert call_args[0][1] == "judgemind-documents-dev"

    def test_expand_takes_screenshots(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        item_dir.mkdir()

        db_response = [
            {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "s3_key": "CA/LA/doc.pdf",
                "s3_bucket": "judgemind-documents-dev",
                "doc_format": "pdf",
                "case_id": "test-case-id",
            }
        ]

        strategy = RulingsStrategy()
        with (
            patch("fetch._run_db_query", return_value=db_response),
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._take_screenshot", return_value=True) as mock_screenshot,
        ):
            result = strategy.expand("a1b2c3d4-e5f6-7890-abcd-ef1234567890", item_dir)

        assert "ruling_screenshot.png" in result["artifacts"]
        assert "case_screenshot.png" in result["artifacts"]
        # Should take 2 screenshots: ruling detail + case detail
        assert mock_screenshot.call_count == 2

    def test_expand_handles_missing_db_record(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "00000000-0000-0000-0000-000000000000"
        item_dir.mkdir()

        strategy = RulingsStrategy()
        with (
            patch("fetch._run_db_query", return_value=[]),
            patch("fetch._fetch_s3_object", return_value=False),
            patch("fetch._take_screenshot", return_value=False),
        ):
            result = strategy.expand("00000000-0000-0000-0000-000000000000", item_dir)

        assert result["id"] == "00000000-0000-0000-0000-000000000000"
        assert "db_record.json" in result["artifacts"]

    def test_expand_html_format(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "b2c3d4e5-f6a7-8901-bcde-f12345678901"
        item_dir.mkdir()

        db_response = [
            {
                "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
                "s3_key": "CA/LA/doc.html",
                "s3_bucket": "judgemind-documents-dev",
                "doc_format": "html",
                "case_id": "test-case-id",
            }
        ]

        strategy = RulingsStrategy()
        with (
            patch("fetch._run_db_query", return_value=db_response),
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("b2c3d4e5-f6a7-8901-bcde-f12345678901", item_dir)

        assert "original.html" in result["artifacts"]


class TestOriginalsStrategy:
    """Tests for the originals expansion strategy."""

    def test_expand_fetches_original_pdf(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "test-doc"
        item_dir.mkdir()

        derived_response = [
            {"ruling_id": "ruling-1", "case_number": "BC123"},
        ]

        strategy = OriginalsStrategy()
        with (
            patch("fetch._fetch_s3_object", return_value=True) as mock_s3,
            patch("fetch._run_db_query", return_value=derived_response),
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("CA/LA/dept/doc.pdf", item_dir)

        assert "original.pdf" in result["artifacts"]
        mock_s3.assert_called_once()

    def test_expand_fetches_derived_rulings(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "test-doc"
        item_dir.mkdir()

        derived_response = [
            {"ruling_id": "ruling-1", "case_number": "BC123"},
            {"ruling_id": "ruling-2", "case_number": "BC456"},
        ]

        strategy = OriginalsStrategy()
        with (
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._run_db_query", return_value=derived_response),
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("CA/LA/dept/doc.pdf", item_dir)

        assert "derived_rulings.json" in result["artifacts"]
        assert (item_dir / "derived_rulings.json").exists()

    def test_expand_takes_ruling_screenshots(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "test-doc"
        item_dir.mkdir()

        derived_response = [
            {"ruling_id": "ruling-1", "case_number": "BC123"},
            {"ruling_id": "ruling-2", "case_number": "BC456"},
        ]

        strategy = OriginalsStrategy()
        with (
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._run_db_query", return_value=derived_response),
            patch("fetch._take_screenshot", return_value=True) as mock_screenshot,
        ):
            result = strategy.expand("CA/LA/dept/doc.pdf", item_dir)

        # Should take 2 screenshots, one per derived ruling
        assert mock_screenshot.call_count == 2
        assert "ruling_screenshots/ruling-1.png" in result["artifacts"]
        assert "ruling_screenshots/ruling-2.png" in result["artifacts"]

    def test_expand_html_format(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "test-doc"
        item_dir.mkdir()

        strategy = OriginalsStrategy()
        with (
            patch("fetch._fetch_s3_object", return_value=True),
            patch("fetch._run_db_query", return_value=[]),
            patch("fetch._take_screenshot", return_value=True),
        ):
            result = strategy.expand("CA/LA/dept/doc.html", item_dir)

        assert "original.html" in result["artifacts"]


class TestFetchManifest:
    """Tests for the main fetch_manifest function."""

    def test_creates_output_structure(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        manifest = {
            "entity": "rulings",
            "county": "Los Angeles",
            "sampled_at": "2026-03-28T00:00:00+00:00",
            "ids": ["uuid-1", "uuid-2"],
        }

        mock_strategy = MagicMock()
        mock_strategy.return_value.expand.return_value = {"id": "x", "artifacts": []}

        with patch.dict(STRATEGIES, {"rulings": mock_strategy}):
            fetch_manifest(manifest, output_dir)

        assert (output_dir / "manifest.json").exists()
        assert (output_dir / "items").is_dir()

    def test_copies_manifest_to_output(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        manifest = {
            "entity": "rulings",
            "county": "Orange",
            "sampled_at": "2026-03-28T00:00:00+00:00",
            "ids": ["uuid-1"],
        }

        mock_strategy = MagicMock()
        mock_strategy.return_value.expand.return_value = {"id": "x", "artifacts": []}

        with patch.dict(STRATEGIES, {"rulings": mock_strategy}):
            fetch_manifest(manifest, output_dir)

        saved = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert saved["entity"] == "rulings"
        assert saved["county"] == "Orange"

    def test_creates_item_directories(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        manifest = {
            "entity": "rulings",
            "county": "Los Angeles",
            "sampled_at": "2026-03-28T00:00:00+00:00",
            "ids": ["aaaaaaaa-1111-2222-3333-444444444444"],
        }

        mock_strategy = MagicMock()
        mock_strategy.return_value.expand.return_value = {"id": "x", "artifacts": []}

        with patch.dict(STRATEGIES, {"rulings": mock_strategy}):
            fetch_manifest(manifest, output_dir)

        items_dir = output_dir / "items"
        item_dirs = list(items_dir.iterdir())
        assert len(item_dirs) == 1
        assert item_dirs[0].name == "aaaaaaaa-1111-2222-3333-444444444444"

    def test_returns_summary(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        manifest = {
            "entity": "rulings",
            "county": "Los Angeles",
            "sampled_at": "2026-03-28T00:00:00+00:00",
            "ids": ["uuid-1", "uuid-2"],
        }

        mock_strategy = MagicMock()
        mock_strategy.return_value.expand.return_value = {
            "id": "x",
            "artifacts": ["db_record.json"],
        }

        with patch.dict(STRATEGIES, {"rulings": mock_strategy}):
            summary = fetch_manifest(manifest, output_dir)

        assert summary["entity"] == "rulings"
        assert summary["total"] == 2
        assert len(summary["items"]) == 2

    def test_exits_on_unknown_entity(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        manifest = {
            "entity": "unknown_type",
            "county": "Los Angeles",
            "ids": ["id-1"],
        }

        with pytest.raises(SystemExit):
            fetch_manifest(manifest, output_dir)
