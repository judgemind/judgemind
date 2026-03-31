"""Tests for Ballotpedia judge biographical data scraper.

Fixtures:
    ballotpedia_judge.html — representative Ballotpedia page for a CA trial
        court judge with Education, Career, Elections, and Biography sections.
    ballotpedia_judge_minimal.html — minimal page with only Education section.
    ballotpedia_not_found.html — search results page for a non-existent article.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from botocore.exceptions import ClientError

from courts.ca.ballotpedia import (
    BASE_URL,
    BallotpediaBioEntry,
    CareerEntry,
    EducationEntry,
    ElectionEntry,
    _parse_single_career_entry,
    _parse_single_education_entry,
    archive_to_s3,
    build_candidate_urls,
    build_external_s3_key,
    fetch_judge_bio,
    is_article_not_found,
    parse_ballotpedia_page,
    rebuild_bio_from_s3,
)
from framework.hashing import sha256_hex

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"

FIXED_TIME = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# build_candidate_urls — unit tests
# ---------------------------------------------------------------------------


class TestBuildCandidateUrls:
    def test_with_middle_initial(self) -> None:
        urls = build_candidate_urls("Maria", "Rodriguez", "T")
        assert urls == [
            f"{BASE_URL}/Maria_T._Rodriguez",
            f"{BASE_URL}/Maria_Rodriguez",
        ]

    def test_without_middle_initial(self) -> None:
        urls = build_candidate_urls("John", "Smith")
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_middle_initial_with_period(self) -> None:
        """Period on middle initial is normalized — only one period in URL."""
        urls = build_candidate_urls("Maria", "Rodriguez", "T.")
        assert urls[0] == f"{BASE_URL}/Maria_T._Rodriguez"

    def test_whitespace_stripped(self) -> None:
        urls = build_candidate_urls(" Maria ", " Rodriguez ", " T ")
        assert urls[0] == f"{BASE_URL}/Maria_T._Rodriguez"
        assert urls[1] == f"{BASE_URL}/Maria_Rodriguez"

    def test_hyphenated_last_name(self) -> None:
        urls = build_candidate_urls("Kerry", "Duffy-Lewis")
        assert urls == [f"{BASE_URL}/Kerry_Duffy-Lewis"]

    def test_empty_middle_initial_treated_as_none(self) -> None:
        urls = build_candidate_urls("John", "Smith", "")
        # Empty string is falsy, so no middle initial URL
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_multi_word_first_name(self) -> None:
        """First names with spaces get underscores in URL."""
        urls = build_candidate_urls("Mary Ann", "Jones")
        assert urls == [f"{BASE_URL}/Mary_Ann_Jones"]

    def test_period_only_middle_initial(self) -> None:
        """A period-only middle initial should not produce a malformed URL."""
        urls = build_candidate_urls("John", "Smith", ".")
        # After stripping the period, mid is empty — should skip middle initial URL
        assert urls == [f"{BASE_URL}/John_Smith"]

    def test_whitespace_only_middle_initial(self) -> None:
        """Whitespace-only middle initial should not produce a malformed URL."""
        urls = build_candidate_urls("John", "Smith", "   ")
        assert urls == [f"{BASE_URL}/John_Smith"]


# ---------------------------------------------------------------------------
# _parse_single_education_entry — unit tests
# ---------------------------------------------------------------------------


class TestParseSingleEducationEntry:
    def test_full_entry(self) -> None:
        entry = _parse_single_education_entry("B.A., University of California, Berkeley, 2005")
        assert entry is not None
        assert entry.degree == "B.A."
        assert entry.institution == "University of California, Berkeley"
        assert entry.year == "2005"

    def test_jd_entry(self) -> None:
        entry = _parse_single_education_entry("J.D., Stanford Law School, 2008")
        assert entry is not None
        assert entry.degree == "J.D."
        assert entry.institution == "Stanford Law School"
        assert entry.year == "2008"

    def test_no_year(self) -> None:
        entry = _parse_single_education_entry("B.A., UCLA")
        assert entry is not None
        assert entry.degree == "B.A."
        assert entry.institution == "UCLA"
        assert entry.year is None

    def test_no_degree(self) -> None:
        entry = _parse_single_education_entry("University of Southern California, 2001")
        assert entry is not None
        assert entry.degree is None
        assert entry.institution == "University of Southern California"
        assert entry.year == "2001"

    def test_empty_string_returns_none(self) -> None:
        assert _parse_single_education_entry("") is None
        assert _parse_single_education_entry("   ") is None

    def test_bs_degree(self) -> None:
        entry = _parse_single_education_entry("B.S., University of Southern California, 2001")
        assert entry is not None
        assert entry.degree == "B.S."
        assert entry.institution == "University of Southern California"
        assert entry.year == "2001"


# ---------------------------------------------------------------------------
# _parse_single_career_entry — unit tests
# ---------------------------------------------------------------------------


class TestParseSingleCareerEntry:
    def test_full_entry(self) -> None:
        entry = _parse_single_career_entry("Associate, Morrison & Foerster LLP, 2008-2012")
        assert entry is not None
        assert entry.position == "Associate"
        assert entry.employer == "Morrison & Foerster LLP"
        assert entry.years == "2008-2012"

    def test_no_years(self) -> None:
        entry = _parse_single_career_entry("Associate, Morrison & Foerster LLP")
        assert entry is not None
        assert entry.position == "Associate"
        assert entry.employer == "Morrison & Foerster LLP"
        assert entry.years is None

    def test_no_employer(self) -> None:
        entry = _parse_single_career_entry("Private practice")
        assert entry is not None
        assert entry.position == "Private practice"
        assert entry.employer is None
        assert entry.years is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_single_career_entry("") is None

    def test_year_range_with_present(self) -> None:
        entry = _parse_single_career_entry("Senior Partner, Smith & Associates, 2018-present")
        assert entry is not None
        assert entry.years == "2018-present"

    def test_single_year(self) -> None:
        entry = _parse_single_career_entry("Clerk, Judge Anderson, 2007")
        assert entry is not None
        assert entry.years == "2007"


# ---------------------------------------------------------------------------
# parse_ballotpedia_page — fixture tests
# ---------------------------------------------------------------------------


class TestParseBallotpediaPage:
    def test_parses_full_page(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"
        assert result.fetched_at == FIXED_TIME

    def test_extracts_biography_text(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert result.biography_text is not None
        assert "Maria T. Rodriguez" in result.biography_text
        assert "Los Angeles County Superior Court" in result.biography_text

    def test_extracts_education(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert len(result.education) == 2

        ba = result.education[0]
        assert ba.degree == "B.A."
        assert "University of California, Berkeley" in ba.institution
        assert ba.year == "2005"

        jd = result.education[1]
        assert jd.degree == "J.D."
        assert "Stanford Law School" in jd.institution
        assert jd.year == "2008"

    def test_extracts_career(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        assert len(result.career) == 3
        assert result.career[0].position == "Associate"
        assert result.career[0].employer == "Morrison & Foerster LLP"
        assert result.career[0].years == "2008-2012"

    def test_extracts_elections(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)

        # Fixture has primary + general election tables under 2024 heading
        assert len(result.elections) == 2

        primary = result.elections[0]
        assert primary.year == "2024"
        assert primary.race is not None
        assert "Primary" in primary.race
        assert len(primary.candidates) == 3  # 3 candidates in primary

        general = result.elections[1]
        assert general.year == "2024"
        assert general.race is not None
        assert "General" in general.race
        assert len(general.candidates) == 2  # 2 candidates in general
        assert "Maria T. Rodriguez" in general.candidates[0]["name"]

    def test_minimal_page_education_only(self) -> None:
        html = _load("ballotpedia_judge_minimal.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/John_Smith", FIXED_TIME)

        assert len(result.education) == 2
        assert result.education[0].institution == "University of Southern California"
        assert result.education[1].institution == "UCLA School of Law"
        assert result.career == []
        assert result.elections == []

    def test_empty_html_returns_empty_entry(self) -> None:
        result = parse_ballotpedia_page(
            "<html><body></body></html>",
            f"{BASE_URL}/test",
            FIXED_TIME,
        )
        assert result.education == []
        assert result.career == []
        assert result.elections == []
        assert result.biography_text is None

    def test_raw_html_stored(self) -> None:
        html = _load("ballotpedia_judge.html")
        result = parse_ballotpedia_page(html, f"{BASE_URL}/Maria_T._Rodriguez", FIXED_TIME)
        assert result.raw_html == html


# ---------------------------------------------------------------------------
# is_article_not_found — unit tests
# ---------------------------------------------------------------------------


class TestIsArticleNotFound:
    def test_not_found_page(self) -> None:
        html = _load("ballotpedia_not_found.html")
        assert is_article_not_found(html) is True

    def test_normal_page(self) -> None:
        html = _load("ballotpedia_judge.html")
        assert is_article_not_found(html) is False

    def test_empty_html(self) -> None:
        assert is_article_not_found("<html><body></body></html>") is False


# ---------------------------------------------------------------------------
# BallotpediaBioEntry.to_bio_source_dict — unit tests
# ---------------------------------------------------------------------------


class TestBioSourceDict:
    def test_includes_required_fields(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/Maria_T._Rodriguez",
            fetched_at=FIXED_TIME,
            biography_text="Some bio text",
            education=[
                EducationEntry(institution="Stanford Law School", degree="J.D.", year="2008"),
            ],
            career=[
                CareerEntry(position="Associate", employer="Big Law LLP", years="2008-2012"),
            ],
            elections=[
                ElectionEntry(year="2024", race="Office 97", candidates=[]),
            ],
        )
        result = entry.to_bio_source_dict()

        assert result["source"] == "ballotpedia"
        assert result["url"] == f"{BASE_URL}/Maria_T._Rodriguez"
        assert result["fetched_at"] == FIXED_TIME.isoformat()
        assert isinstance(result["content"], dict)

    def test_content_structure(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/test",
            fetched_at=FIXED_TIME,
            education=[
                EducationEntry(institution="Harvard Law", degree="J.D.", year="2010"),
            ],
            career=[],
            elections=[],
        )
        content = entry.to_bio_source_dict()["content"]

        assert isinstance(content, dict)
        assert len(content["education"]) == 1
        assert content["education"][0]["institution"] == "Harvard Law"
        assert content["education"][0]["degree"] == "J.D."
        assert content["education"][0]["year"] == "2010"
        assert content["career"] == []
        assert content["elections"] == []

    def test_empty_entry(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/test",
            fetched_at=FIXED_TIME,
        )
        result = entry.to_bio_source_dict()
        assert result["content"]["education"] == []
        assert result["content"]["career"] == []
        assert result["content"]["elections"] == []
        assert result["content"]["biography_text"] is None


# ---------------------------------------------------------------------------
# fetch_judge_bio — mocked HTTP tests
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_bio_with_middle_initial() -> None:
    """fetch_judge_bio with middle initial tries that URL first."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("Maria", "Rodriguez", "T", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"
    assert len(result.education) == 2
    assert len(result.career) == 3
    assert result.raw_html is not None
    assert "Maria T. Rodriguez" in result.raw_html


@respx.mock
def test_fetch_bio_falls_back_to_no_middle_initial() -> None:
    """When the middle initial URL 404s, try without middle initial."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}/Maria_Rodriguez").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("Maria", "Rodriguez", "T", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/Maria_Rodriguez"


@respx.mock
def test_fetch_bio_returns_none_for_nonexistent_judge() -> None:
    """fetch_judge_bio returns None when no page exists."""
    not_found_html = _load("ballotpedia_not_found.html")
    respx.get(f"{BASE_URL}/Zzzzz_Q._Fakename").mock(
        return_value=httpx.Response(200, text=not_found_html)
    )
    respx.get(f"{BASE_URL}/Zzzzz_Fakename").mock(
        return_value=httpx.Response(200, text=not_found_html)
    )

    result = fetch_judge_bio("Zzzzz", "Fakename", "Q", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_without_middle_initial() -> None:
    """fetch_judge_bio without middle initial only tries one URL."""
    html = _load("ballotpedia_judge_minimal.html")
    respx.get(f"{BASE_URL}/John_Smith").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio("John", "Smith", request_delay=0)
    assert result is not None
    assert result.source_url == f"{BASE_URL}/John_Smith"


@respx.mock
def test_fetch_bio_handles_404_gracefully() -> None:
    """404 responses don't raise — just return None."""
    respx.get(f"{BASE_URL}/Nobody_Here").mock(return_value=httpx.Response(404))

    result = fetch_judge_bio("Nobody", "Here", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_handles_http_error() -> None:
    """HTTP errors (network failures) don't raise — just return None."""
    respx.get(f"{BASE_URL}/Network_Fail").mock(side_effect=httpx.ConnectError("Connection refused"))

    result = fetch_judge_bio("Network", "Fail", request_delay=0)
    assert result is None


@respx.mock
def test_fetch_bio_handles_server_error() -> None:
    """500 errors are handled gracefully (no exceptions)."""
    respx.get(f"{BASE_URL}/Server_Error").mock(return_value=httpx.Response(500))

    result = fetch_judge_bio("Server", "Error", request_delay=0)
    assert result is None


# ---------------------------------------------------------------------------
# build_external_s3_key — unit tests
# ---------------------------------------------------------------------------


class TestBuildExternalS3Key:
    def test_key_structure(self) -> None:
        key = build_external_s3_key("abc123def456")
        assert key == "external/ballotpedia/abc123def456.html"

    def test_key_with_real_hash(self) -> None:
        content_hash = sha256_hex(b"<html>test</html>")
        key = build_external_s3_key(content_hash)
        assert key == f"external/ballotpedia/{content_hash}.html"
        assert len(content_hash) == 64  # SHA-256 hex digest length


# ---------------------------------------------------------------------------
# archive_to_s3 — unit tests
# ---------------------------------------------------------------------------


class TestArchiveToS3:
    def test_archives_new_page(self) -> None:
        """New page is uploaded to S3 with correct key and metadata."""
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        html = "<html><body>Judge Bio</body></html>"
        key = archive_to_s3(
            html,
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert key is not None
        content_hash = sha256_hex(html.encode("utf-8"))
        assert key == f"external/ballotpedia/{content_hash}.html"

        mock_client.head_object.assert_called_once()
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == key
        assert call_kwargs["Body"] == html.encode("utf-8")
        assert call_kwargs["ContentType"] == "text/html"
        assert call_kwargs["Metadata"]["source"] == "ballotpedia"
        assert call_kwargs["Metadata"]["source-url"] == f"{BASE_URL}/Test_Judge"
        assert call_kwargs["Metadata"]["content-hash"] == content_hash
        assert call_kwargs["Metadata"]["fetched-at"] == FIXED_TIME.isoformat()

    def test_skips_upload_when_exists(self) -> None:
        """HeadObject success means page is already archived — skip upload."""
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 1234}

        html = "<html><body>Judge Bio</body></html>"
        key = archive_to_s3(
            html,
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert key is not None
        mock_client.head_object.assert_called_once()
        mock_client.put_object.assert_not_called()

    def test_returns_none_when_no_bucket(self) -> None:
        """Returns None when bucket is not configured."""
        key = archive_to_s3(
            "<html>test</html>",
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
            bucket="",
        )
        assert key is None

    def test_returns_none_when_bucket_none_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when bucket is None and env var is not set."""
        monkeypatch.delenv("JUDGEMIND_ARCHIVE_BUCKET", raising=False)
        key = archive_to_s3(
            "<html>test</html>",
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
        )
        assert key is None

    def test_raises_on_head_object_non_404_error(self) -> None:
        """Non-404 HeadObject errors are re-raised."""
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        with pytest.raises(ClientError):
            archive_to_s3(
                "<html>test</html>",
                source_url=f"{BASE_URL}/Test_Judge",
                fetched_at=FIXED_TIME,
                s3_client=mock_client,
                bucket="test-bucket",
            )

    def test_raises_on_put_object_error(self) -> None:
        """PutObject errors are re-raised."""
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        mock_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": ""}}, "PutObject"
        )

        with pytest.raises(ClientError):
            archive_to_s3(
                "<html>test</html>",
                source_url=f"{BASE_URL}/Test_Judge",
                fetched_at=FIXED_TIME,
                s3_client=mock_client,
                bucket="test-bucket",
            )

    def test_content_addressed_idempotency(self) -> None:
        """Same content produces the same S3 key."""
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        html = "<html><body>Same Content</body></html>"
        key1 = archive_to_s3(
            html,
            source_url=f"{BASE_URL}/Judge_One",
            fetched_at=FIXED_TIME,
            s3_client=mock_client,
            bucket="test-bucket",
        )
        key2 = archive_to_s3(
            html,
            source_url=f"{BASE_URL}/Judge_Two",
            fetched_at=FIXED_TIME,
            s3_client=mock_client,
            bucket="test-bucket",
        )
        assert key1 == key2

    def test_uses_fixture_html(self) -> None:
        """archive_to_s3 works with real fixture HTML."""
        mock_client = MagicMock()
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )

        html = _load("ballotpedia_judge.html")
        key = archive_to_s3(
            html,
            source_url=f"{BASE_URL}/Maria_T._Rodriguez",
            fetched_at=FIXED_TIME,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert key is not None
        assert key.startswith("external/ballotpedia/")
        assert key.endswith(".html")
        # Verify the content hash in the key matches what sha256_hex produces
        content_hash = sha256_hex(html.encode("utf-8"))
        assert key == f"external/ballotpedia/{content_hash}.html"


# ---------------------------------------------------------------------------
# rebuild_bio_from_s3 — unit tests
# ---------------------------------------------------------------------------


class TestRebuildBioFromS3:
    def test_rebuilds_from_metadata(self) -> None:
        """rebuild_bio_from_s3 reads source_url and fetched_at from S3 metadata."""
        html = _load("ballotpedia_judge.html")
        content_hash = sha256_hex(html.encode("utf-8"))
        s3_key = f"external/ballotpedia/{content_hash}.html"

        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": io.BytesIO(html.encode("utf-8")),
            "Metadata": {
                "source": "ballotpedia",
                "source-url": f"{BASE_URL}/Maria_T._Rodriguez",
                "fetched-at": FIXED_TIME.isoformat(),
                "content-hash": content_hash,
            },
        }

        result = rebuild_bio_from_s3(
            s3_key,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"
        assert result.s3_key == s3_key
        assert result.fetched_at == FIXED_TIME
        assert len(result.education) == 2
        assert len(result.career) == 3
        assert result.biography_text is not None
        assert result.raw_html == html

        mock_client.get_object.assert_called_once_with(Bucket="test-bucket", Key=s3_key)

    def test_parameter_overrides_metadata(self) -> None:
        """Explicit source_url and fetched_at override S3 metadata."""
        html = _load("ballotpedia_judge.html")
        content_hash = sha256_hex(html.encode("utf-8"))
        s3_key = f"external/ballotpedia/{content_hash}.html"

        override_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": io.BytesIO(html.encode("utf-8")),
            "Metadata": {
                "source-url": f"{BASE_URL}/Maria_T._Rodriguez",
                "fetched-at": FIXED_TIME.isoformat(),
            },
        }

        result = rebuild_bio_from_s3(
            s3_key,
            source_url=f"{BASE_URL}/Override_URL",
            s3_client=mock_client,
            bucket="test-bucket",
            fetched_at=override_time,
        )

        assert result.source_url == f"{BASE_URL}/Override_URL"
        assert result.fetched_at == override_time

    def test_rebuilds_minimal_page(self) -> None:
        """rebuild_bio_from_s3 works with minimal page fixture."""
        html = _load("ballotpedia_judge_minimal.html")
        content_hash = sha256_hex(html.encode("utf-8"))
        s3_key = f"external/ballotpedia/{content_hash}.html"

        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": io.BytesIO(html.encode("utf-8")),
            "Metadata": {
                "source-url": f"{BASE_URL}/John_Smith",
                "fetched-at": FIXED_TIME.isoformat(),
            },
        }

        result = rebuild_bio_from_s3(
            s3_key,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert result.s3_key == s3_key
        assert result.fetched_at == FIXED_TIME
        assert len(result.education) == 2
        assert result.career == []
        assert result.elections == []

    def test_falls_back_to_last_modified(self) -> None:
        """Uses S3 LastModified as fallback when fetched-at metadata is missing."""
        html = _load("ballotpedia_judge.html")
        content_hash = sha256_hex(html.encode("utf-8"))
        s3_key = f"external/ballotpedia/{content_hash}.html"
        last_modified = datetime(2026, 2, 15, 8, 30, 0, tzinfo=UTC)

        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": io.BytesIO(html.encode("utf-8")),
            "Metadata": {
                "source-url": f"{BASE_URL}/Maria_T._Rodriguez",
                # No fetched-at in metadata
            },
            "LastModified": last_modified,
        }

        result = rebuild_bio_from_s3(
            s3_key,
            s3_client=mock_client,
            bucket="test-bucket",
        )

        assert result.fetched_at == last_modified
        assert result.source_url == f"{BASE_URL}/Maria_T._Rodriguez"

    def test_raises_when_no_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Raises ValueError when bucket is not configured."""
        monkeypatch.delenv("JUDGEMIND_ARCHIVE_BUCKET", raising=False)
        with pytest.raises(ValueError, match="JUDGEMIND_ARCHIVE_BUCKET not set"):
            rebuild_bio_from_s3(
                "external/ballotpedia/abc123.html",
            )

    def test_raises_when_no_source_url(self) -> None:
        """Raises ValueError when source_url is not in params or metadata."""
        mock_client = MagicMock()
        mock_client.get_object.return_value = {
            "Body": io.BytesIO(b"<html>test</html>"),
            "Metadata": {},
        }

        with pytest.raises(ValueError, match="source_url not provided"):
            rebuild_bio_from_s3(
                "external/ballotpedia/abc123.html",
                s3_client=mock_client,
                bucket="test-bucket",
            )

    def test_raises_on_s3_error(self) -> None:
        """S3 errors when fetching the object are re-raised."""
        mock_client = MagicMock()
        mock_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )

        with pytest.raises(ClientError):
            rebuild_bio_from_s3(
                "external/ballotpedia/nonexistent.html",
                source_url=f"{BASE_URL}/Test_Judge",
                s3_client=mock_client,
                bucket="test-bucket",
            )


# ---------------------------------------------------------------------------
# fetch_judge_bio with S3 archival — mocked HTTP + S3 tests
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_bio_archives_to_s3() -> None:
    """fetch_judge_bio archives the fetched page to S3 before parsing."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(200, text=html))

    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    result = fetch_judge_bio(
        "Maria",
        "Rodriguez",
        "T",
        request_delay=0,
        s3_client=mock_s3,
        bucket="test-bucket",
    )

    assert result is not None
    assert result.s3_key is not None
    assert result.s3_key.startswith("external/ballotpedia/")
    assert result.s3_key.endswith(".html")

    # Verify S3 put_object was called with fetched_at metadata
    mock_s3.put_object.assert_called_once()
    call_kwargs = mock_s3.put_object.call_args.kwargs
    assert call_kwargs["Bucket"] == "test-bucket"
    assert call_kwargs["ContentType"] == "text/html"
    assert "fetched-at" in call_kwargs["Metadata"]
    # fetched_at should be a valid ISO timestamp
    fetched_at_str = call_kwargs["Metadata"]["fetched-at"]
    assert "T" in fetched_at_str  # basic ISO format check


@respx.mock
def test_fetch_bio_skips_s3_when_no_bucket() -> None:
    """fetch_judge_bio works without S3 archival when bucket is empty."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(200, text=html))

    result = fetch_judge_bio(
        "Maria",
        "Rodriguez",
        "T",
        request_delay=0,
        bucket="",
    )

    assert result is not None
    assert result.s3_key is None
    assert len(result.education) == 2


@respx.mock
def test_fetch_bio_s3_key_in_bio_source_dict() -> None:
    """S3 key is included in bio source dict when archival succeeds."""
    html = _load("ballotpedia_judge.html")
    respx.get(f"{BASE_URL}/Maria_T._Rodriguez").mock(return_value=httpx.Response(200, text=html))

    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    result = fetch_judge_bio(
        "Maria",
        "Rodriguez",
        "T",
        request_delay=0,
        s3_client=mock_s3,
        bucket="test-bucket",
    )

    assert result is not None
    bio_dict = result.to_bio_source_dict()
    assert "s3_key" in bio_dict
    assert bio_dict["s3_key"] == result.s3_key


# ---------------------------------------------------------------------------
# BallotpediaBioEntry.to_bio_source_dict with s3_key — unit tests
# ---------------------------------------------------------------------------


class TestBioSourceDictWithS3Key:
    def test_includes_s3_key_when_set(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
            s3_key="external/ballotpedia/abc123.html",
        )
        result = entry.to_bio_source_dict()
        assert result["s3_key"] == "external/ballotpedia/abc123.html"

    def test_omits_s3_key_when_none(self) -> None:
        entry = BallotpediaBioEntry(
            source_url=f"{BASE_URL}/Test_Judge",
            fetched_at=FIXED_TIME,
        )
        result = entry.to_bio_source_dict()
        assert "s3_key" not in result
