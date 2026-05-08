"""Unit tests for ``ingestion.case_type_resolver.resolve_case_type``.

The resolver replaces an open-coded fallback chain that previously lived
both in ``packages/scraper-framework/src/ingestion/worker.py`` and in
``_apply_regex_fallbacks`` inside ``scripts/reingest_from_s3.py``.  See
issue #4295 (parent #4290).

The tests below exercise every branch of the chain in the canonical
order — case_number → scraper_id → motion_type → case_title — plus the
``case_type already set`` early-return, the all-misses-returns-None
case, and the priority interactions.
"""

from __future__ import annotations

from ingestion.case_type_resolver import resolve_case_type


class TestResolveCaseTypeAlreadySet:
    """When ``case_type`` is already set, the resolver returns it verbatim."""

    def test_returns_existing_case_type_unchanged(self) -> None:
        result = resolve_case_type(
            case_type="family",
            case_number="CIVSB2501234",  # would otherwise resolve to civil
            scraper_id="ca-oc-tentatives-probate",
            motion_type="msj",
            case_title="In the Matter of John Doe",
        )
        assert result == ("family", None), (
            "Expected pre-set case_type to be returned with method=None — "
            "the resolver must not re-run any fallback when case_type is "
            "already populated."
        )

    def test_returns_existing_case_type_with_no_fallback_inputs(self) -> None:
        result = resolve_case_type(
            case_type="probate",
            case_number=None,
            scraper_id=None,
            motion_type=None,
            case_title=None,
        )
        assert result == ("probate", None)


class TestResolveCaseTypeFromCaseNumber:
    """First fallback step — case_number prefix."""

    def test_civil_case_number_resolves(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number="CIVSB2501234",
            scraper_id=None,
            motion_type=None,
            case_title=None,
        )
        assert result == ("civil", "regex")

    def test_family_case_number_resolves(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number="FL2301234",
            scraper_id=None,
            motion_type=None,
            case_title=None,
        )
        assert result == ("family", "regex")

    def test_probate_case_number_resolves(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number="PR2301234",
            scraper_id=None,
            motion_type=None,
            case_title=None,
        )
        assert result == ("probate", "regex")

    def test_unrecognised_case_number_falls_through(self) -> None:
        """Case number that doesn't match any prefix pattern moves on to
        the next fallback step."""
        result = resolve_case_type(
            case_type=None,
            case_number="UNKNOWN123",
            scraper_id="ca-oc-tentatives-civil",
            motion_type=None,
            case_title=None,
        )
        assert result == ("civil", "scraper_id")


class TestResolveCaseTypeFromScraperId:
    """Second fallback step — scraper_id suffix."""

    def test_civil_scraper_id_resolves(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id="ca-oc-tentatives-civil",
            motion_type=None,
            case_title=None,
        )
        assert result == ("civil", "scraper_id")

    def test_probate_scraper_id_resolves(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id="ca-oc-tentatives-probate",
            motion_type=None,
            case_title=None,
        )
        assert result == ("probate", "scraper_id")

    def test_unrecognised_scraper_id_falls_through(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id="ca-mystery-scraper",
            motion_type="msj",
            case_title=None,
        )
        assert result == ("civil", "motion_type")


class TestResolveCaseTypeFromMotionType:
    """Third fallback step — motion_type."""

    def test_msj_resolves_civil(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type="msj",
            case_title=None,
        )
        assert result == ("civil", "motion_type")

    def test_request_for_order_resolves_family(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type="request_for_order",
            case_title=None,
        )
        assert result == ("family", "motion_type")

    def test_unrecognised_motion_type_falls_through(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type="ex_parte_application",  # ambiguous, returns None
            case_title="In the Matter of Jane Doe",
        )
        assert result == ("probate", "title")


class TestResolveCaseTypeFromTitle:
    """Fourth (final) fallback step — case_title."""

    def test_in_the_matter_of_resolves_probate(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type=None,
            case_title="In the Matter of John Smith",
        )
        assert result == ("probate", "title")

    def test_conservatorship_of_resolves_probate(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type=None,
            case_title="Conservatorship of Jane Doe",
        )
        assert result == ("probate", "title")

    def test_estate_of_resolves_probate(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type=None,
            case_title="Estate of Mary Roe",
        )
        assert result == ("probate", "title")


class TestResolveCaseTypeAllMissesReturnsNone:
    """When every signal misses, the resolver returns ``(None, None)``."""

    def test_no_inputs_returns_none(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type=None,
            case_title=None,
        )
        assert result == (None, None)

    def test_all_unrecognised_inputs_returns_none(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number="UNRECOGNIZED999",
            scraper_id="generic-scraper-with-no-suffix",
            motion_type="ex_parte_application",  # ambiguous
            case_title="Smith v. Jones",  # generic civil title, no probate cue
        )
        assert result == (None, None)

    def test_empty_strings_treated_as_missing(self) -> None:
        """Empty / whitespace-only strings should be treated as missing
        signals — falsy values short-circuit each step."""
        result = resolve_case_type(
            case_type=None,
            case_number="",
            scraper_id="",
            motion_type="",
            case_title="",
        )
        assert result == (None, None)


class TestResolveCaseTypePriority:
    """The fallback order encodes confidence — case_number wins over
    scraper_id, scraper_id wins over motion_type, etc."""

    def test_case_number_beats_scraper_id(self) -> None:
        """``CIVSB`` -> civil, even when scraper_id encodes probate.
        Mirrors ``test_fallback_parity.py::test_case_type_priority_number_over_scraper_id``."""
        result = resolve_case_type(
            case_type=None,
            case_number="CIVSB2501234",
            scraper_id="ca-oc-tentatives-probate",
            motion_type=None,
            case_title=None,
        )
        assert result == ("civil", "regex")

    def test_scraper_id_beats_motion_type(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id="ca-oc-tentatives-probate",
            motion_type="msj",  # would resolve civil
            case_title=None,
        )
        assert result == ("probate", "scraper_id")

    def test_motion_type_beats_title(self) -> None:
        result = resolve_case_type(
            case_type=None,
            case_number=None,
            scraper_id=None,
            motion_type="msj",
            case_title="In the Matter of John Doe",  # would resolve probate
        )
        assert result == ("civil", "motion_type")
