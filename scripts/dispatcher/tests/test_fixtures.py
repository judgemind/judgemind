"""Round-trip tests for the ``_fixtures`` JSONB helper module.

Issue #2874 — AC1 (helper exists) + AC4 (fixture round-trip).

Tests confirm that:
- ``psycopg_jsonb`` is the identity function for native Python values.
- ``psycopg_jsonb_legacy`` returns the JSON-encoded string form.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.tests._fixtures import psycopg_jsonb, psycopg_jsonb_legacy  # noqa: E402


class TestPsycopgJsonbIsIdentityForNativeValues:
    """AC1 / AC4: ``psycopg_jsonb`` is the identity function."""

    def test_string_round_trip(self) -> None:
        assert psycopg_jsonb("circuit_breaker") == "circuit_breaker"

    def test_bool_true_is_true(self) -> None:
        assert psycopg_jsonb(True) is True

    def test_bool_false_is_false(self) -> None:
        assert psycopg_jsonb(False) is False

    def test_int_round_trip(self) -> None:
        assert psycopg_jsonb(5) == 5

    def test_none_is_none(self) -> None:
        assert psycopg_jsonb(None) is None

    def test_float_round_trip(self) -> None:
        assert psycopg_jsonb(0.30) == 0.30

    def test_dict_round_trip(self) -> None:
        d = {"action": "retry"}
        assert psycopg_jsonb(d) is d

    def test_list_round_trip(self) -> None:
        lst = [1, 2, 3]
        assert psycopg_jsonb(lst) is lst


class TestPsycopgJsonbLegacyJsonEncodes:
    """AC4: ``psycopg_jsonb_legacy`` returns the JSON-encoded wire shape."""

    def test_string_is_json_quoted(self) -> None:
        assert psycopg_jsonb_legacy("circuit_breaker") == '"circuit_breaker"'

    def test_bool_true_is_lowercase_true(self) -> None:
        assert psycopg_jsonb_legacy(True) == "true"

    def test_bool_false_is_lowercase_false(self) -> None:
        assert psycopg_jsonb_legacy(False) == "false"

    def test_int_is_string(self) -> None:
        assert psycopg_jsonb_legacy(5) == "5"

    def test_none_is_null(self) -> None:
        assert psycopg_jsonb_legacy(None) == "null"

    def test_float_encodes(self) -> None:
        assert psycopg_jsonb_legacy(0.30) == "0.3"


class TestFixtureContrast:
    """Contrast the two shapes to confirm they are distinct."""

    def test_string_shapes_differ(self) -> None:
        """psycopg_jsonb returns the bare string; legacy adds JSON quotes."""
        assert psycopg_jsonb("circuit_breaker") != psycopg_jsonb_legacy(
            "circuit_breaker"
        )

    def test_bool_shapes_differ(self) -> None:
        """psycopg_jsonb returns Python bool; legacy returns a JSON string."""
        assert type(psycopg_jsonb(True)) is bool
        assert type(psycopg_jsonb_legacy(True)) is str
