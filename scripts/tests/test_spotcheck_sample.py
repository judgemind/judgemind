"""Tests for scripts/spotcheck/sample.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spotcheck"))

from sample import SAMPLERS, build_manifest, main


class TestBuildManifest:
    """Tests for the manifest builder."""

    def test_builds_valid_manifest(self) -> None:
        ids = ["uuid-1", "uuid-2", "uuid-3"]
        manifest = build_manifest("rulings", "Los Angeles", ids)

        assert manifest["entity"] == "rulings"
        assert manifest["county"] == "Los Angeles"
        assert manifest["ids"] == ids
        assert "sampled_at" in manifest

    def test_sampled_at_is_iso_format(self) -> None:
        manifest = build_manifest("rulings", "Orange", ["id-1"])
        # Should be valid ISO 8601
        assert "T" in manifest["sampled_at"]
        assert "+" in manifest["sampled_at"] or "Z" in manifest["sampled_at"]

    def test_empty_ids_list(self) -> None:
        manifest = build_manifest("originals", "Orange", [])
        assert manifest["ids"] == []
        assert manifest["entity"] == "originals"


class TestSamplerRegistry:
    """Tests for the sampler registry."""

    def test_rulings_registered(self) -> None:
        assert "rulings" in SAMPLERS

    def test_originals_registered(self) -> None:
        assert "originals" in SAMPLERS


class TestSampleRulings:
    """Tests for the rulings sampler."""

    def test_queries_db_and_returns_ids(self) -> None:
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("uuid-aaa",),
            ("uuid-bbb",),
            ("uuid-ccc",),
        ]
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        with (
            patch.dict("sys.modules", {"psycopg": mock_psycopg}),
            patch.dict("os.environ", {"DATABASE_URL": "postgres://test"}),
        ):
            result = SAMPLERS["rulings"]("Los Angeles", 3)

        assert result == ["uuid-aaa", "uuid-bbb", "uuid-ccc"]
        mock_cursor.execute.assert_called_once()
        # Verify the SQL contains county filter and limit
        sql_arg = mock_cursor.execute.call_args[0][0]
        assert "county" in sql_arg.lower()
        assert "random" in sql_arg.lower()

    def test_exits_without_database_url(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            # Remove DATABASE_URL
            import os

            os.environ.pop("DATABASE_URL", None)
            with pytest.raises(SystemExit):
                SAMPLERS["rulings"]("Los Angeles", 5)


class TestSampleOriginals:
    """Tests for the originals sampler."""

    def test_lists_s3_keys_and_samples(self) -> None:
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "CA/Los Angeles/doc1.pdf"},
                    {"Key": "CA/Los Angeles/doc2.pdf"},
                    {"Key": "CA/Los Angeles/doc3.pdf"},
                    {"Key": "CA/Los Angeles/doc4.html"},
                    {"Key": "CA/Los Angeles/doc5.pdf"},
                ],
            },
        ]

        mock_s3 = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            result = SAMPLERS["originals"]("Los Angeles", 3)

        assert len(result) == 3
        # All results should be from the original keys
        all_keys = {
            "CA/Los Angeles/doc1.pdf",
            "CA/Los Angeles/doc2.pdf",
            "CA/Los Angeles/doc3.pdf",
            "CA/Los Angeles/doc4.html",
            "CA/Los Angeles/doc5.pdf",
        }
        for key in result:
            assert key in all_keys

    def test_returns_empty_when_no_objects(self) -> None:
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]

        mock_s3 = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            result = SAMPLERS["originals"]("Nonexistent", 5)

        assert result == []

    def test_limits_to_available_count(self) -> None:
        """If n > available items, returns all available."""
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "CA/Orange/doc1.pdf"},
                    {"Key": "CA/Orange/doc2.pdf"},
                ]
            },
        ]

        mock_s3 = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            result = SAMPLERS["originals"]("Orange", 100)

        assert len(result) == 2


class TestMainCli:
    """Tests for the CLI entry point."""

    def test_outputs_manifest_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        test_args = [
            "sample.py",
            "--from",
            "rulings",
            "--county",
            "Los Angeles",
            "--n",
            "2",
        ]

        mock_sampler = MagicMock(return_value=["uuid-1", "uuid-2"])

        with (
            patch("sys.argv", test_args),
            patch.dict(SAMPLERS, {"rulings": mock_sampler}),
        ):
            main()

        output = capsys.readouterr().out
        manifest = json.loads(output)
        assert manifest["entity"] == "rulings"
        assert manifest["county"] == "Los Angeles"
        assert len(manifest["ids"]) == 2

    def test_writes_manifest_to_file(self, tmp_path: Path) -> None:
        output_file = tmp_path / "manifest.json"
        test_args = [
            "sample.py",
            "--from",
            "originals",
            "--county",
            "Orange",
            "--n",
            "1",
            "--output",
            str(output_file),
        ]

        mock_sampler = MagicMock(return_value=["CA/Orange/doc1.pdf"])

        with (
            patch("sys.argv", test_args),
            patch.dict(SAMPLERS, {"originals": mock_sampler}),
        ):
            main()

        assert output_file.exists()
        manifest = json.loads(output_file.read_text(encoding="utf-8"))
        assert manifest["entity"] == "originals"
        assert manifest["ids"] == ["CA/Orange/doc1.pdf"]

    def test_exits_when_no_results(self) -> None:
        test_args = [
            "sample.py",
            "--from",
            "rulings",
            "--county",
            "Nonexistent",
            "--n",
            "5",
        ]

        mock_sampler = MagicMock(return_value=[])

        with (
            patch("sys.argv", test_args),
            patch.dict(SAMPLERS, {"rulings": mock_sampler}),
            pytest.raises(SystemExit),
        ):
            main()
