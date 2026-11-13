"""``_should_move_off_unsupported_case`` (#4915): when a reingest moves a
whole document's ruling off a case number the document does not print."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from ingestion.worker import REPLACE_SPLIT_SET_KEY, _should_move_off_unsupported_case

_TEXT = "Tentative ruling. Case CVRI2401570. " + "The motion is granted. " * 10


def _conn(current: str | None) -> MagicMock:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (current,) if current is not None else None
    return conn


def _write(event: dict[str, Any], texts: tuple[str, ...] = (_TEXT,)) -> Any:
    return SimpleNamespace(event_data=event, document_id="doc-1", case_number_texts=texts)


_REINGEST = {REPLACE_SPLIT_SET_KEY: True}


@pytest.mark.parametrize(
    ("event", "texts", "current", "expected"),
    [
        # The held number is not printed: move.
        (_REINGEST, (_TEXT,), "CVRI2401073", True),
        # The held number is printed: keep (#4788).
        (_REINGEST, (_TEXT,), "CVRI2401570", False),
        # The link text carries it: keep.
        (
            {**_REINGEST, "extra": {"link_text": "CVRI2401073 ruling"}},
            (_TEXT,),
            "CVRI2401073",
            False,
        ),
        # No ruling on another case, or a placeholder one: nothing to move.
        (_REINGEST, (_TEXT,), None, False),
        (_REINGEST, (_TEXT,), "UNKNOWN-doc-1", False),
        # A live write, a split child, or no checkable text: never.
        ({}, (_TEXT,), "CVRI2401073", False),
        ({**_REINGEST, "_split_processed": True}, (_TEXT,), "CVRI2401073", False),
        (_REINGEST, (), "CVRI2401073", False),
    ],
)
def test_should_move_off_unsupported_case(
    event: dict[str, Any], texts: tuple[str, ...], current: str | None, expected: bool
) -> None:
    assert _should_move_off_unsupported_case(_conn(current), _write(event, texts), "case-new") is (
        expected
    )
