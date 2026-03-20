"""Tests for the document splitting framework (ingestion/splitter.py).

Tests cover:
  - SplitResult dataclass construction
  - split_document() pass-through when no splitter is registered
  - split_document() pass-through when splitter returns single result
  - split_document() dispatch to registered county splitter
  - split_document() graceful handling of splitter exceptions
  - make_split_document_id() determinism and uniqueness
  - register_splitter() overwrite behavior
  - Registration completeness: all splitter modules are auto-imported
"""

from __future__ import annotations

from typing import Any

from ingestion.splitter import (
    SplitResult,
    _splitter_registry,
    make_split_document_id,
    register_splitter,
    split_document,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict[str, Any]:
    """Return a minimal valid event payload for splitting tests."""
    base: dict[str, Any] = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "state": "CA",
        "county": "TestCounty",
        "ruling_text": "The motion is GRANTED.",
        "case_number": "23STCV12345",
        "case_title": "Smith v. Jones",
        "motion_type": "msj",
        "outcome": "granted",
    }
    base.update(overrides)
    return base


def _mock_splitter(event_data: dict[str, Any]) -> list[SplitResult]:
    """A mock splitter that splits text on '---' delimiter."""
    ruling_text = event_data.get("ruling_text", "")
    parts = ruling_text.split("---")
    if len(parts) < 2:
        return [SplitResult(ruling_text=ruling_text)]
    return [
        SplitResult(
            ruling_text=part.strip(),
            case_title=f"Case {i + 1}",
            case_number=f"CASE-{i + 1:04d}",
        )
        for i, part in enumerate(parts)
        if part.strip()
    ]


def _failing_splitter(event_data: dict[str, Any]) -> list[SplitResult]:
    """A splitter that always raises an exception."""
    raise ValueError("Splitter failed")


# ---------------------------------------------------------------------------
# SplitResult tests
# ---------------------------------------------------------------------------


class TestSplitResult:
    def test_required_field_only(self) -> None:
        result = SplitResult(ruling_text="Some text")
        assert result.ruling_text == "Some text"
        assert result.case_title is None
        assert result.case_number is None
        assert result.motion_type is None
        assert result.outcome is None

    def test_all_fields(self) -> None:
        result = SplitResult(
            ruling_text="Some text",
            case_title="Smith v. Jones",
            case_number="23STCV12345",
            motion_type="msj",
            outcome="granted",
        )
        assert result.ruling_text == "Some text"
        assert result.case_title == "Smith v. Jones"
        assert result.case_number == "23STCV12345"
        assert result.motion_type == "msj"
        assert result.outcome == "granted"


# ---------------------------------------------------------------------------
# split_document() tests
# ---------------------------------------------------------------------------


class TestSplitDocument:
    def setup_method(self) -> None:
        """Clear the splitter registry before each test."""
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        """Restore the splitter registry after each test."""
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    def test_no_splitter_registered_passes_through(self) -> None:
        """When no splitter is registered for the county, returns single result."""
        event = _make_event()
        results = split_document(event)
        assert len(results) == 1
        assert results[0].ruling_text == "The motion is GRANTED."
        assert results[0].case_title == "Smith v. Jones"
        assert results[0].case_number == "23STCV12345"

    def test_no_ruling_text_passes_through(self) -> None:
        """When ruling_text is empty, returns single result with empty text."""
        event = _make_event(ruling_text="")
        results = split_document(event)
        assert len(results) == 1
        assert results[0].ruling_text == ""

    def test_none_ruling_text_passes_through(self) -> None:
        """When ruling_text is None, returns single result."""
        event = _make_event(ruling_text=None)
        results = split_document(event)
        assert len(results) == 1

    def test_splitter_returns_single_result(self) -> None:
        """When the splitter returns one result, no splitting occurs."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        event = _make_event(ruling_text="No delimiter here")
        results = split_document(event)
        assert len(results) == 1
        assert results[0].ruling_text == "No delimiter here"

    def test_splitter_returns_multiple_results(self) -> None:
        """When the splitter returns multiple results, document is split."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        event = _make_event(ruling_text="Case one text --- Case two text --- Case three text")
        results = split_document(event)
        assert len(results) == 3
        assert results[0].ruling_text == "Case one text"
        assert results[0].case_title == "Case 1"
        assert results[0].case_number == "CASE-0001"
        assert results[1].ruling_text == "Case two text"
        assert results[1].case_title == "Case 2"
        assert results[2].ruling_text == "Case three text"
        assert results[2].case_title == "Case 3"

    def test_splitter_returns_empty_list(self) -> None:
        """When the splitter returns empty list, falls back to pass-through."""

        def empty_splitter(event_data: dict[str, Any]) -> list[SplitResult]:
            return []

        register_splitter("CA", "TestCounty", empty_splitter)
        event = _make_event()
        results = split_document(event)
        assert len(results) == 1
        assert results[0].ruling_text == "The motion is GRANTED."

    def test_splitter_exception_falls_back_to_passthrough(self) -> None:
        """When the splitter raises, logs the error and passes through."""
        register_splitter("CA", "TestCounty", _failing_splitter)
        event = _make_event()
        results = split_document(event)
        assert len(results) == 1
        assert results[0].ruling_text == "The motion is GRANTED."

    def test_county_dispatch(self) -> None:
        """The correct splitter is selected based on state and county."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        # Event with a different county should NOT use the TestCounty splitter
        event = _make_event(county="OtherCounty")
        results = split_document(event)
        assert len(results) == 1  # No splitter for OtherCounty

    def test_state_dispatch(self) -> None:
        """The splitter dispatch checks both state and county."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        event = _make_event(state="NY", county="TestCounty")
        results = split_document(event)
        assert len(results) == 1  # No splitter for NY/TestCounty


# ---------------------------------------------------------------------------
# make_split_document_id() tests
# ---------------------------------------------------------------------------


class TestMakeSplitDocumentId:
    def test_deterministic(self) -> None:
        """Same inputs always produce the same output."""
        doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
        result1 = make_split_document_id(doc_id, 0)
        result2 = make_split_document_id(doc_id, 0)
        assert result1 == result2

    def test_different_indices_produce_different_ids(self) -> None:
        """Different split indices produce different document IDs."""
        doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
        id0 = make_split_document_id(doc_id, 0)
        id1 = make_split_document_id(doc_id, 1)
        id2 = make_split_document_id(doc_id, 2)
        assert id0 != id1
        assert id1 != id2
        assert id0 != id2

    def test_different_documents_produce_different_ids(self) -> None:
        """Different original document IDs produce different split IDs."""
        id_a = make_split_document_id("aaaaaaaa-0000-0000-0000-000000000001", 0)
        id_b = make_split_document_id("bbbbbbbb-0000-0000-0000-000000000002", 0)
        assert id_a != id_b

    def test_returns_valid_uuid(self) -> None:
        """The result is a valid UUID string."""
        import uuid

        result = make_split_document_id("aaaaaaaa-0000-0000-0000-000000000001", 0)
        # Should not raise
        parsed = uuid.UUID(result)
        assert str(parsed) == result


# ---------------------------------------------------------------------------
# register_splitter() tests
# ---------------------------------------------------------------------------


class TestRegisterSplitter:
    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    def test_register_and_lookup(self) -> None:
        """Registered splitter is used by split_document."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        assert ("CA", "TestCounty") in _splitter_registry

    def test_overwrite_warning(self) -> None:
        """Re-registering for the same county logs a warning."""
        register_splitter("CA", "TestCounty", _mock_splitter)
        # Second registration should overwrite without error
        register_splitter("CA", "TestCounty", _mock_splitter)
        assert ("CA", "TestCounty") in _splitter_registry


# ---------------------------------------------------------------------------
# Registration completeness test
# ---------------------------------------------------------------------------


class TestSplitterRegistrationCompleteness:
    """Verify all splitter modules are auto-imported by _load_county_splitters().

    If a developer adds a new splitter module with a register_splitter() call
    but forgets to import it in _load_county_splitters(), this test will fail.
    """

    def test_all_splitter_modules_are_registered(self) -> None:
        """Every register_splitter() call in src/ingestion/ is reachable."""
        import ast
        from pathlib import Path

        # Find the ingestion source directory.
        ingestion_dir = Path(__file__).resolve().parent.parent / "src" / "ingestion"
        assert ingestion_dir.is_dir(), f"Expected directory: {ingestion_dir}"

        # Scan all Python files in ingestion/ for register_splitter() calls.
        expected_pairs: set[tuple[str, str]] = set()
        for py_file in sorted(ingestion_dir.glob("*.py")):
            # Skip the module that *defines* register_splitter.
            if py_file.name == "splitter.py":
                continue

            source = py_file.read_text()
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Match both `register_splitter(...)` and `splitter.register_splitter(...)`.
                func = node.func
                name: str | None = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name != "register_splitter":
                    continue
                # Extract (state, county) from the first two positional args.
                if len(node.args) < 2:
                    continue
                state_arg = node.args[0]
                county_arg = node.args[1]
                if isinstance(state_arg, ast.Constant) and isinstance(county_arg, ast.Constant):
                    expected_pairs.add((state_arg.value, county_arg.value))

        # Sanity: we should have found at least the known splitters.
        assert len(expected_pairs) >= 2, (
            f"Expected at least 2 register_splitter() calls in src/ingestion/, "
            f"found {len(expected_pairs)}: {expected_pairs}"
        )

        # After importing ingestion.splitter (which happens at the top of this
        # file), _load_county_splitters() has already run.  Verify every
        # expected (state, county) pair is in the registry.
        missing = expected_pairs - set(_splitter_registry.keys())
        assert not missing, (
            f"The following splitter(s) have register_splitter() calls but are "
            f"NOT imported by _load_county_splitters() in splitter.py: {missing}. "
            f"Add the missing import(s) to _load_county_splitters()."
        )
