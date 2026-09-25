"""FetchTally: the shared "every per-item fetch failed" gate (#4693).

Most court scrapers fetch a list of items (department PDFs, dropdown
options, case lookups) and catch each item's exception so that one bad item
does not lose the rest. The catch is correct, but it creates a silent-outage
failure mode. When *every* item raises, ``fetch_documents`` returns ``[]`` and
``BaseScraper.run()`` records ``status=success records=0``, which looks the
same as a quiet court day. Tentative rulings disappear within days, so an
outage recorded as success loses them for good.

``FetchTally`` counts the outcome of each item and turns "nothing captured and
no attempt succeeded" into a :class:`ScraperPreconditionFailure`. The runner
records that as ``success=False`` with a readable ``error_message``.

Usage::

    tally = FetchTally("PDF fetches")
    for href in links:
        tally.attempt()
        try:
            ...
            if looks_like_block_page(body):
                tally.blocked("block page")
                continue
            docs.append(doc)
        except Exception as exc:
            tally.failed(exc)
            log.error(...)
    tally.raise_if_all_failed(docs)
    return docs

Semantics:

- ``attempt()`` starts an item. An attempt that is never marked ``failed`` or
  ``blocked`` counts as succeeded, including one that exits early via
  ``continue`` because it legitimately found nothing.
- ``failed(exc)`` and ``blocked(reason)`` mark the current attempt. Called
  without an open attempt, they count a new attempt of their own.
- ``ok()`` records an explicit success; it closes the open attempt or counts a
  new one.
- Pick one style per tally. Flat loops use ``attempt()`` plus ``failed()`` /
  ``blocked()``. Nested loops (a listing page, then its items) use explicit
  ``ok()`` / ``failed()`` pairs, because a standalone ``failed()`` for the
  outer page would otherwise land on an inner item's still-open attempt.
- ``raise_if_all_failed(docs)`` raises only when ``docs`` is empty, at least
  one attempt was made, and none succeeded. A fetch that completes and finds
  nothing (a genuinely empty calendar) stays a success, and so does a run
  with nothing to attempt.
- ``abort(reason, remaining=N)`` records that the loop stopped early with *N*
  items never attempted (a circuit breaker, e.g. SD's 5-strike streak). An
  abort that skips items is never a green run (#4734):

  - with no docs, ``raise_if_all_failed`` raises even if some attempts
    succeeded;
  - with docs, raising would discard them (``BaseScraper.run()`` archives
    only after ``fetch_documents`` returns), so the scraper passes
    :meth:`partial_failure_message` to ``BaseScraper._mark_partial_failure``
    instead. ``run()`` archives the docs, then records ``success=False``.

  A scraper with a breaker ends its fetch with::

      tally.raise_if_all_failed(docs)
      self._mark_partial_failure(tally.partial_failure_message())

The semantics match the hand-rolled SD ROA counters from #4673 and #4687,
which were the first user.
"""

from __future__ import annotations

from collections.abc import Sized
from typing import Any

from .base import ScraperPreconditionFailure

_MAX_ERROR_LEN = 300


def _describe(error: BaseException | str) -> str:
    """``Type: first line`` of an exception, or the reason string, truncated.

    Only the first line is kept: httpx appends a "For more information" line
    and Playwright appends a multi-line call log, neither useful in a
    one-line run error.
    """
    lines = str(error).strip().splitlines()
    first = lines[0] if lines else ""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {first}" if first else type(error).__name__
    else:
        text = first
    return text[:_MAX_ERROR_LEN]


class FetchTally:
    """Count per-item fetch outcomes and fail the run when none succeeded."""

    def __init__(self, what: str = "fetches") -> None:
        # Plural noun phrase used in the failure message, e.g. "PDF fetches".
        self.what = what
        self.n_attempted = 0
        self.n_failed = 0
        self.n_blocked = 0
        self.last_error: str | None = None
        self.last_block_reason: str | None = None
        # Set by abort(): why the loop stopped and how many items it skipped.
        self.abort_reason: str | None = None
        self.n_skipped = 0
        self._open = False

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def attempt(self) -> None:
        """Start a new item attempt. Unmarked attempts count as succeeded."""
        self.n_attempted += 1
        self._open = True

    def _close_or_count(self) -> None:
        if self._open:
            self._open = False
        else:
            self.n_attempted += 1

    def ok(self) -> None:
        """Record an explicit success."""
        self._close_or_count()

    def failed(self, error: BaseException | str) -> None:
        """Mark the current attempt as failed with *error*."""
        self._close_or_count()
        self.n_failed += 1
        self.last_error = _describe(error)

    def blocked(self, reason: str) -> None:
        """Mark the current attempt as blocked (anti-bot page, expired session)."""
        self._close_or_count()
        self.n_blocked += 1
        self.last_block_reason = _describe(reason)

    def abort(self, reason: str, *, remaining: int) -> None:
        """Record that the loop stopped early with *remaining* items unattempted."""
        self.abort_reason = _describe(reason)
        self.n_skipped = max(remaining, 0)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def aborted(self) -> bool:
        """True when :meth:`abort` skipped at least one item."""
        return self.abort_reason is not None and self.n_skipped > 0

    @property
    def n_ok(self) -> int:
        return self.n_attempted - self.n_failed - self.n_blocked

    @property
    def all_failed(self) -> bool:
        """True when at least one attempt was made and none succeeded."""
        return self.n_attempted > 0 and self.n_ok <= 0

    def log_fields(self) -> dict[str, Any]:
        """Counts as structlog keyword arguments."""
        return {
            "attempted": self.n_attempted,
            "succeeded": self.n_ok,
            "blocked": self.n_blocked,
            "failed": self.n_failed,
            "skipped": self.n_skipped,
            "last_error": self.last_error or self.last_block_reason,
        }

    def failure_message(self) -> str:
        """One-line reason for a run in which no attempt succeeded."""
        if not self.n_failed:
            return (
                f"all {self.n_attempted} {self.what} were blocked; "
                f"last block reason: {self.last_block_reason}"
            )
        message = (
            f"all {self.n_attempted} {self.what} failed "
            f"({self.n_blocked} blocked, {self.n_failed} raised errors); "
            f"last error: {self.last_error}"
        )
        if self.n_blocked:
            message += f"; last block reason: {self.last_block_reason}"
        return message

    def abort_message(self) -> str:
        """One-line reason for a run whose loop aborted with items left."""
        total = self.n_attempted + self.n_skipped
        message = (
            f"{self.what} aborted after {self.n_attempted} of {total}, "
            f"{self.n_skipped} skipped ({self.n_ok} succeeded, "
            f"{self.n_blocked} blocked, {self.n_failed} raised errors): "
            f"{self.abort_reason}"
        )
        if self.last_error:
            message += f"; last error: {self.last_error}"
        if self.last_block_reason:
            message += f"; last block reason: {self.last_block_reason}"
        return message

    def partial_failure_message(self) -> str | None:
        """:meth:`abort_message` when items were skipped, else ``None``.

        Pass it to ``BaseScraper._mark_partial_failure`` so a run that
        captured some docs but skipped others is recorded as failed after
        the captured docs are archived.
        """
        return self.abort_message() if self.aborted else None

    # ------------------------------------------------------------------
    # Gate
    # ------------------------------------------------------------------

    def raise_if_all_failed(self, docs: Sized, *, message: str | None = None) -> None:
        """Raise :class:`ScraperPreconditionFailure` if nothing was captured
        and either every attempt failed or the loop aborted with items left.

        *message* replaces the default :meth:`failure_message` when a scraper
        has a more specific diagnosis (e.g. SD's anti-bot summary). It applies
        only to the all-failed case; an abort after some successes always
        reports :meth:`abort_message`.
        """
        if len(docs) != 0:
            return
        if self.all_failed:
            text = message or self.failure_message()
            if self.aborted:
                text += f"; {self.n_skipped} more skipped after abort"
            raise ScraperPreconditionFailure(text)
        if self.aborted:
            raise ScraperPreconditionFailure(self.abort_message())
