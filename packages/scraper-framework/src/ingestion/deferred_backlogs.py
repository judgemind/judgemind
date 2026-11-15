"""Raw-key backlogs whose reingest is deliberately on hold (#5036, #5043).

A deferred backlog is a set of archived raw keys that must not go through
the ingestion worker until a blocking issue is decided.  The one entry today
is the May-June 2026 CourtListener raws (#3954): the reingest path cannot
read the CourtListener envelope's case fields, so a reingest fails
validation and the worker replaces each key's split set with an empty one,
deleting the key's existing rulings (#5043: 106 federal rulings were lost
this way during #5026).

This module is the single definition.  Every tool that can feed raw keys to
the worker reads it:

- ``scripts/reconcile_raw_keys.py`` reports those keys as "deferred
  (blocked on #N)" and never re-ingests them;
- ``scripts/reingest_from_s3.py`` and ``scripts/rebuild_db.py`` skip them by
  default (``deferred_backlog_skipped``) and process them only with
  ``--include-deferred``.

It lives in the package, not in ``scripts/``, because ECS oneshot scripts
cannot import other ``scripts/*.py`` files.  Remove an entry once its
blocking issue is decided and its keys can go back to normal handling.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

#: Exit code of a reingest or rebuild whose every selected key was deferred:
#: nothing ran, and the operator should know why (#5043).
ONLY_DEFERRED_EXIT_CODE = 5

#: ``<state>/<county>/<court>/raw/...``: the scope is ``<state>/<county>``.
_RAW_SCOPE_RE = re.compile(r"^(?P<state>[^/]+)/(?P<county>[^/]+)/[^/]+/raw/")


class _ScopedCapture(Protocol):
    """What :meth:`DeferredBacklog.matches` reads from a raw key."""

    @property
    def scope(self) -> str: ...

    @property
    def utc(self) -> datetime: ...


@dataclass(frozen=True)
class DeferredBacklog:
    """Raw keys whose reingest is deliberately on hold behind *issue*.

    They are real losses, not skips: the reconciler keeps them
    ``unrepresented`` but reports them as "deferred (blocked on #<issue>)",
    never re-ingests them and never lets them make a scope
    ``over_threshold``.  A scope (``<state>/<county>``) matches when it fits
    one of *scopes* and none of *exclude* (``fnmatch`` patterns).  With
    *captured_before*, only keys captured (S3 ``LastModified``) before that
    day are deferred: the known backlog waits, while a later loss in the
    same scope heals as usual.

    Intentional skips (the governor bio route, ``telemetry.ingest_skips``)
    are not deferrals: they are never meant to produce a document.
    """

    name: str
    issue: int
    scopes: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    captured_before: date | None = None

    def matches_scope(self, scope: str) -> bool:
        """Whether *scope* (``<state>/<county>``) is in this backlog's scopes."""
        if not any(fnmatch.fnmatchcase(scope, p) for p in self.scopes):
            return False
        return not any(fnmatch.fnmatchcase(scope, p) for p in self.exclude)

    def matches_capture(self, captured_at: datetime | None) -> bool:
        """Whether a key captured at *captured_at* falls in the backlog's window.

        An unknown capture time (``None``) counts as inside the window: when
        the key's age cannot be read, skipping it is the safe side.
        """
        if self.captured_before is None or captured_at is None:
            return True
        ts = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=UTC)
        return ts.astimezone(UTC).date() < self.captured_before

    def matches_key(self, scope: str, captured_at: datetime | None) -> bool:
        return self.matches_scope(scope) and self.matches_capture(captured_at)

    def matches(self, raw: _ScopedCapture) -> bool:
        """The reconciler's form: *raw* has ``scope`` and ``utc``."""
        return self.matches_key(raw.scope, raw.utc)

    @property
    def label(self) -> str:
        return f"deferred (blocked on #{self.issue})"


#: Deliberately deferred backlogs (#5036).  CourtListener raws (``federal/``,
#: ``unknown/`` and ``<state name>/statewide``; the ``ca/`` capture prefix is
#: never CourtListener) lost in the May-June 2026 ElastiCache window (#4470)
#: wait on the operator decision #3954 (reingest tracked by #5022).  Every one
#: was captured by 2026-06-18; a later CourtListener loss heals as usual.
DEFERRED_BACKLOGS: tuple[DeferredBacklog, ...] = (
    DeferredBacklog(
        name="courtlistener_oom_window",
        issue=3954,
        scopes=("federal/*", "unknown/*", "*/statewide"),
        exclude=("ca/*",),
        captured_before=date(2026, 6, 21),
    ),
)


def raw_key_scope(key: str) -> str | None:
    """``<state>/<county>`` of a raw key, or ``None`` for another layout."""
    m = _RAW_SCOPE_RE.match(key)
    return f"{m['state']}/{m['county']}" if m else None


def backlogs_for_scope(
    scope: str, backlogs: Sequence[DeferredBacklog] = DEFERRED_BACKLOGS
) -> list[DeferredBacklog]:
    """The backlogs that may hold keys of *scope* (capture time aside)."""
    return [b for b in backlogs if b.matches_scope(scope)]


@dataclass
class DeferralSplit:
    """Keys split into the ones to process and the deferred ones."""

    kept: list[str] = field(default_factory=list)
    #: backlog name -> deferred keys, input order kept.
    deferred: dict[str, list[str]] = field(default_factory=dict)
    #: True when ``--include-deferred`` kept the deferred keys in ``kept``.
    included: bool = False

    @property
    def deferred_count(self) -> int:
        return sum(len(v) for v in self.deferred.values())

    @property
    def only_deferred(self) -> bool:
        """Every input key was deferred and skipped: nothing to process."""
        return not self.included and not self.kept and self.deferred_count > 0


def split_deferred_keys(
    keys: Iterable[str],
    capture_time: Callable[[str], datetime | None],
    *,
    include_deferred: bool = False,
    backlogs: Sequence[DeferredBacklog] = DEFERRED_BACKLOGS,
) -> DeferralSplit:
    """Split *keys* into processable and deferred ones.

    *capture_time* is called only for keys whose scope matches a backlog
    (so a caller may HEAD the object without touching every key); ``None``
    means unknown, which counts as deferred.  With *include_deferred* the
    deferred keys stay in ``kept`` too, and are still counted.
    """
    split = DeferralSplit(included=include_deferred)
    for key in keys:
        scope = raw_key_scope(key)
        candidates = backlogs_for_scope(scope, backlogs) if scope else []
        backlog = None
        if candidates:
            captured = capture_time(key)
            backlog = next((b for b in candidates if b.matches_capture(captured)), None)
        if backlog is not None:
            split.deferred.setdefault(backlog.name, []).append(key)
            if not include_deferred:
                continue
        split.kept.append(key)
    return split


def log_deferral_split(
    log: Any,
    split: DeferralSplit,
    *,
    backlogs: Sequence[DeferredBacklog] = DEFERRED_BACKLOGS,
    **context: Any,
) -> None:
    """Log what :func:`split_deferred_keys` did, one line per backlog.

    Skipped keys log ``deferred_backlog_skipped`` (warning).  Keys kept by
    ``--include-deferred`` log ``deferred_backlog_included`` (error level, so
    it stands out): the run is processing keys whose reingest is on hold.
    """
    by_name = {b.name: b for b in backlogs}
    for name, keys in split.deferred.items():
        backlog = by_name.get(name)
        fields = {
            "backlog": name,
            "blocking_issue": backlog.issue if backlog else None,
            "count": len(keys),
            "sample": keys[:5],
            **context,
        }
        if split.included:
            log.error(
                "deferred_backlog_included",
                warning=(
                    f"--include-deferred: processing {len(keys)} key(s) whose "
                    f"reingest is on hold behind #{fields['blocking_issue']}. "
                    "A failed reingest replaces each key's rulings with nothing."
                ),
                **fields,
            )
        else:
            log.warning(
                "deferred_backlog_skipped",
                hint=(
                    f"reingest blocked on #{fields['blocking_issue']}; "
                    "pass --include-deferred to process anyway"
                ),
                **fields,
            )
