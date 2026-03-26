"""Tests for the validation issue filer (validation/issue_filer.py).

All GitHub API calls are mocked so these tests run offline in CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from validation.issue_filer import (
    _build_issue_body,
    _is_duplicate,
    file_validation_issue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
) -> httpx.Response:
    """Create a mock httpx Response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = str(json_data)
    return response


# ---------------------------------------------------------------------------
# Tests for file_validation_issue
# ---------------------------------------------------------------------------


class TestFileValidationIssue:
    """Tests for the file_validation_issue function."""

    def test_returns_none_when_no_github_token(self) -> None:
        """No GITHUB_TOKEN should skip issue filing."""
        with patch.dict("os.environ", {}, clear=True):
            result = file_validation_issue(
                result="flag",
                reason="test reason",
                county="Los Angeles",
                case_number="23STCV12345",
                document_id="doc-123",
                s3_key=None,
                ruling_text_excerpt=None,
                github_token=None,
            )
        assert result is None

    def test_creates_issue_for_flag(self) -> None:
        """Flag result should create a GitHub issue with correct labels."""
        mock_client = MagicMock(spec=httpx.Client)

        # First call: search (no duplicates)
        search_response = _make_mock_response(200, {"total_count": 0})
        # Second call: pattern search (no duplicates)
        pattern_search_response = _make_mock_response(200, {"total_count": 0})
        # Third call: create issue
        create_response = _make_mock_response(
            201, {"html_url": "https://github.com/judgemind/judgemind/issues/999"}
        )
        mock_client.get.side_effect = [search_response, pattern_search_response]
        mock_client.post.return_value = create_response

        result = file_validation_issue(
            result="flag",
            reason="Case title mismatch",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key="ca/los_angeles/raw/doc.html",
            ruling_text_excerpt="The motion is GRANTED.",
            github_token="test-token",
            http_client=mock_client,
        )

        assert result == "https://github.com/judgemind/judgemind/issues/999"
        # Verify the POST was called
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "data-quality-failure" in payload["labels"]
        assert "priority/p2" in payload["labels"]
        assert "type/bug" in payload["labels"]
        assert "area/scraping" in payload["labels"]
        # Flag should NOT have agent/ready
        assert "agent/ready" not in payload["labels"]

    def test_creates_issue_for_fail_with_agent_ready(self) -> None:
        """Fail result should create a GitHub issue with agent/ready label."""
        mock_client = MagicMock(spec=httpx.Client)

        search_response = _make_mock_response(200, {"total_count": 0})
        pattern_search_response = _make_mock_response(200, {"total_count": 0})
        create_response = _make_mock_response(
            201, {"html_url": "https://github.com/judgemind/judgemind/issues/1000"}
        )
        mock_client.get.side_effect = [search_response, pattern_search_response]
        mock_client.post.return_value = create_response

        result = file_validation_issue(
            result="fail",
            reason="Wrong case content",
            county="Orange",
            case_number=None,
            document_id="doc-456",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        assert result is not None
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "priority/p1" in payload["labels"]
        assert "agent/ready" in payload["labels"]

    def test_skips_duplicate_by_document_id(self) -> None:
        """Should skip filing if an issue already exists for the same document."""
        mock_client = MagicMock(spec=httpx.Client)

        # Search returns a match
        search_response = _make_mock_response(200, {"total_count": 1})
        mock_client.get.return_value = search_response

        result = file_validation_issue(
            result="flag",
            reason="test reason",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        assert result is None
        # Should NOT have called POST
        mock_client.post.assert_not_called()

    def test_skips_duplicate_by_pattern(self) -> None:
        """Should skip filing if an issue with same county+reason pattern exists."""
        mock_client = MagicMock(spec=httpx.Client)

        # First search (by document_id): no match
        doc_search_response = _make_mock_response(200, {"total_count": 0})
        # Second search (by pattern): match found
        pattern_search_response = _make_mock_response(200, {"total_count": 1})
        mock_client.get.side_effect = [doc_search_response, pattern_search_response]

        result = file_validation_issue(
            result="flag",
            reason="Case title mismatch with ruling text",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-789",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        assert result is None
        mock_client.post.assert_not_called()

    def test_handles_github_api_error_gracefully(self) -> None:
        """GitHub API errors should not raise — returns None."""
        mock_client = MagicMock(spec=httpx.Client)

        search_response = _make_mock_response(200, {"total_count": 0})
        pattern_search_response = _make_mock_response(200, {"total_count": 0})
        # Create returns an error
        create_response = _make_mock_response(500, {"message": "Internal Server Error"})
        mock_client.get.side_effect = [search_response, pattern_search_response]
        mock_client.post.return_value = create_response

        result = file_validation_issue(
            result="flag",
            reason="test reason",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        assert result is None

    def test_handles_network_exception_gracefully(self) -> None:
        """Network exceptions should not raise — returns None."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        result = file_validation_issue(
            result="flag",
            reason="test reason",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        assert result is None

    def test_truncates_long_title(self) -> None:
        """Very long reason strings should be truncated in the title."""
        mock_client = MagicMock(spec=httpx.Client)

        search_response = _make_mock_response(200, {"total_count": 0})
        pattern_search_response = _make_mock_response(200, {"total_count": 0})
        create_response = _make_mock_response(
            201, {"html_url": "https://github.com/judgemind/judgemind/issues/1001"}
        )
        mock_client.get.side_effect = [search_response, pattern_search_response]
        mock_client.post.return_value = create_response

        long_reason = "A" * 200
        file_validation_issue(
            result="flag",
            reason=long_reason,
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key=None,
            ruling_text_excerpt=None,
            github_token="test-token",
            http_client=mock_client,
        )

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert len(payload["title"]) <= 150


# ---------------------------------------------------------------------------
# Tests for _build_issue_body
# ---------------------------------------------------------------------------


class TestBuildIssueBody:
    """Tests for _build_issue_body helper."""

    def test_includes_all_fields(self) -> None:
        body = _build_issue_body(
            result="flag",
            reason="Case title mismatch",
            county="Los Angeles",
            case_number="23STCV12345",
            document_id="doc-123",
            s3_key="ca/los_angeles/raw/doc.html",
            ruling_text_excerpt="The motion is GRANTED.",
            extracted_fields={"case_number": "23STCV12345", "judge_name": "Smith"},
        )
        assert "FLAG" in body
        assert "Case title mismatch" in body
        assert "Los Angeles" in body
        assert "23STCV12345" in body
        assert "doc-123" in body
        assert "ca/los_angeles/raw/doc.html" in body
        assert "The motion is GRANTED." in body
        assert "Smith" in body

    def test_handles_missing_optional_fields(self) -> None:
        body = _build_issue_body(
            result="fail",
            reason="Wrong content",
            county="Orange",
            case_number=None,
            document_id="doc-456",
            s3_key=None,
            ruling_text_excerpt=None,
            extracted_fields=None,
        )
        assert "FAIL" in body
        assert "Orange" in body
        assert "doc-456" in body
        # Should not crash on None fields

    def test_escapes_pipe_in_field_values(self) -> None:
        body = _build_issue_body(
            result="flag",
            reason="test",
            county="Test",
            case_number=None,
            document_id="doc",
            s3_key=None,
            ruling_text_excerpt=None,
            extracted_fields={"title": "Foo | Bar"},
        )
        assert "Foo \\| Bar" in body


# ---------------------------------------------------------------------------
# Tests for _is_duplicate
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    """Tests for _is_duplicate helper."""

    def test_returns_true_when_document_id_matches(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = _make_mock_response(200, {"total_count": 1})

        result = _is_duplicate(
            client=mock_client,
            token="test-token",
            repo="judgemind/judgemind",
            document_id="doc-123",
            county="Los Angeles",
            reason="test reason",
        )
        assert result is True

    def test_returns_false_when_no_matches(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.return_value = _make_mock_response(200, {"total_count": 0})

        result = _is_duplicate(
            client=mock_client,
            token="test-token",
            repo="judgemind/judgemind",
            document_id="doc-123",
            county="Los Angeles",
            reason="test reason",
        )
        assert result is False

    def test_returns_false_on_api_error(self) -> None:
        """API errors during dedup check should not block issue creation."""
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        result = _is_duplicate(
            client=mock_client,
            token="test-token",
            repo="judgemind/judgemind",
            document_id="doc-123",
            county="Los Angeles",
            reason="test reason",
        )
        assert result is False
