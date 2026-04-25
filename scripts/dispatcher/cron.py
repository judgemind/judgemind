"""Minimal cron expression matcher for dispatcher idle-hook scheduling.

Issue #2864 — /audit and /spotcheck idle-hook triggers.

Supported syntax
----------------
Five-field expressions only: ``MIN HOUR DOM MONTH DOW``

Each field accepts:
  * ``*``                — matches any value
  * A fixed integer      — e.g. ``14``, ``0``
  * A comma-separated list of integers — e.g. ``0,15,30,45``

NOT supported (raises ``ValueError`` with a clear message):
  * Step syntax: ``*/5``, ``0/15``
  * Range syntax: ``1-5``, ``MON-FRI``
  * Named months/days: ``JAN``, ``MON``, etc.

Future expansions (step, range, named) belong in this file.  The
``cron_matches`` function is the only public API.

Examples::

    from dispatcher.cron import cron_matches
    from datetime import datetime, timezone

    # Fire at 14:00 UTC on any day
    expr = "0 14 * * *"
    dt   = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert cron_matches(expr, dt) is True

    # Does NOT fire at 14:01
    dt2 = datetime(2024, 6, 15, 14, 1, tzinfo=timezone.utc)
    assert cron_matches(expr, dt2) is False

    # Comma list — fire at :00 and :30 of every hour
    assert cron_matches("0,30 * * * *", datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc)) is True
    assert cron_matches("0,30 * * * *", datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)) is True
    assert cron_matches("0,30 * * * *", datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)) is False
"""

from __future__ import annotations

from datetime import datetime


def _parse_field(raw: str, field_name: str, lo: int, hi: int) -> frozenset[int]:
    """Parse a single cron field; return the set of matching integer values.

    Parameters
    ----------
    raw:        The raw field string from the cron expression.
    field_name: Human-readable name used in error messages (e.g. ``"minute"``).
    lo:         Minimum valid value (inclusive).
    hi:         Maximum valid value (inclusive).
    """
    raw = raw.strip()

    # Wildcard — matches every value in [lo, hi].
    if raw == "*":
        return frozenset(range(lo, hi + 1))

    # Reject step syntax (*/N or N/M) before trying integer parse.
    if "/" in raw:
        raise ValueError(
            f"cron field '{field_name}': step syntax ('{raw}') is not supported. "
            "Only '*', fixed integers, and comma-separated lists are accepted."
        )

    # Reject range syntax (N-M).
    if "-" in raw:
        raise ValueError(
            f"cron field '{field_name}': range syntax ('{raw}') is not supported. "
            "Only '*', fixed integers, and comma-separated lists are accepted."
        )

    # Comma-separated list or single integer.
    parts = raw.split(",")
    values: set[int] = set()
    for part in parts:
        part = part.strip()
        if not part.isdigit():
            # Named months / weekdays or garbage.
            raise ValueError(
                f"cron field '{field_name}': named values or non-integer token "
                f"'{part}' in '{raw}' are not supported. "
                "Only '*', fixed integers, and comma-separated integers are accepted."
            )
        val = int(part)
        if val < lo or val > hi:
            raise ValueError(
                f"cron field '{field_name}': value {val} is out of range [{lo}, {hi}]."
            )
        values.add(val)

    return frozenset(values)


def cron_matches(expr: str, dt: datetime) -> bool:
    """Return True iff *expr* matches the minute-truncated datetime *dt*.

    Parameters
    ----------
    expr:
        A 5-field cron expression string (see module docstring for the
        supported subset).
    dt:
        A timezone-aware (or naive UTC) :class:`~datetime.datetime`.
        Seconds and microseconds are ignored — matching is minute-level.

    Raises
    ------
    ValueError
        When *expr* uses unsupported syntax (steps, ranges, named tokens)
        or has the wrong number of fields.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression must have exactly 5 fields, got {len(parts)}: '{expr}'"
        )

    min_field, hour_field, dom_field, month_field, dow_field = parts

    minutes = _parse_field(min_field, "minute", 0, 59)
    hours = _parse_field(hour_field, "hour", 0, 23)
    doms = _parse_field(dom_field, "day-of-month", 1, 31)
    months = _parse_field(month_field, "month", 1, 12)
    dows_raw = _parse_field(dow_field, "day-of-week", 0, 7)

    # ISO weekday: Monday=1 … Sunday=7.  Cron weekday: Sunday=0 or 7,
    # Monday=1 … Saturday=6.  Normalise so both 0 and 7 mean Sunday.
    # In the dows set, replace 7 with 0 so the comparison uses the
    # canonical cron-style Sunday=0 representation.
    dows: frozenset[int] = frozenset((v % 7 for v in dows_raw))

    dt_dow_cron = dt.isoweekday() % 7  # Sun=0, Mon=1 … Sat=6

    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and dt_dow_cron in dows
    )
