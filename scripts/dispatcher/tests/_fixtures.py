"""Shared JSONB test fixtures for dispatcher unit tests.

Issue #2874: canonical helpers that model what psycopg3 hands back for
a JSONB column vs what legacy fake-cursor tests may feed.

**Production shape** (``psycopg_jsonb``):
  psycopg3 decodes JSONB columns to their native Python equivalents
  before returning rows.  A JSONB string ``"circuit_breaker"`` becomes
  the Python string ``circuit_breaker`` (no surrounding quotes); JSONB
  ``true`` becomes Python ``True``; JSONB number ``5`` becomes Python
  ``int`` 5; JSONB ``null`` becomes Python ``None``.  The fixture
  models this by returning the value unchanged — i.e. the identity
  function — because callers should already be working with native
  Python values when constructing ``fetch_queue`` entries.

**Legacy shape** (``psycopg_jsonb_legacy``):
  Some existing fake-cursor tests were written before psycopg3 and pass
  the raw JSON-encoded wire bytes instead of the unwrapped Python
  value.  For example, the JSON string ``"circuit_breaker"`` was stored
  as the Python string ``'"circuit_breaker"'`` (with the JSON quotes).
  This fixture models that legacy encoding via ``json.dumps``.  It
  exists so tests can exercise the tolerant ``isinstance(raw, str) +
  json.loads`` fallback branches that each JSONB reader still carries
  for backwards compatibility with fake-cursor tests.

Only ``psycopg_jsonb`` models what production callers see.
``psycopg_jsonb_legacy`` is a test-infrastructure artefact — callers
of the legacy shape produce the same reader output, confirming that the
fallback gate works correctly, but the production code path is the
identity form.

See also: ``daemon.py::_read_cap_flipped_by`` docstring at line
16131–16140 for the matching in-daemon rationale.
"""

from __future__ import annotations

import json
from typing import Any


def psycopg_jsonb(value: Any) -> Any:
    """Return *value* in the shape psycopg3 hands back for a JSONB column.

    psycopg3 decodes JSONB scalars to their native Python equivalents
    before returning rows, so this fixture is the identity function for
    values that are already native Python objects.

    Examples::

        psycopg_jsonb("circuit_breaker")  # → "circuit_breaker"
        psycopg_jsonb(True)               # → True
        psycopg_jsonb(5)                  # → 5
        psycopg_jsonb(None)               # → None
    """
    return value


def psycopg_jsonb_legacy(value: Any) -> str:
    """Return *value* encoded as a raw JSON string (legacy fake-cursor shape).

    Some tests written before psycopg3 pass the raw JSONB wire bytes
    instead of the unwrapped Python value.  This fixture models that
    encoding via :func:`json.dumps` so the tolerant
    ``isinstance(raw, str) + json.loads`` fallback branches in each
    JSONB reader can be exercised.

    Examples::

        psycopg_jsonb_legacy("circuit_breaker")  # → '"circuit_breaker"'
        psycopg_jsonb_legacy(True)               # → 'true'
        psycopg_jsonb_legacy(5)                  # → '5'
    """
    return json.dumps(value)
