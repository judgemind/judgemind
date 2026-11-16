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

- The raised :class:`AllFetchesFailed` carries a whole-run retry verdict
  (#4713). ``BaseScraper.run()`` retries the fetch once only when every
  failure was transient (see :func:`is_transient_fetch_error`, and
  ``blocked(reason, transient=True)`` for a stale session) and the failed
  attempt was short. Blocks, 4xx, parse errors and aborts are not retried.

The semantics match the hand-rolled SD ROA counters from #4673 and #4687,
which were the first user.

Non-aborting per-item failures count against the run (#5019):

- Every tally created while ``BaseScraper.run()`` is fetching registers
  itself with the run (see :func:`collect_fetch_tallies`); no scraper has to
  opt in.  After the fetch, ``run()`` adds each tally's ``failed`` and
  ``blocked`` items to ``records_failed``, puts the counts and up to 50
  per-item samples (URL, error, HTTP status, transient / permanent) in
  ``scraper_runs.error_details``, and records ``success=False``.  One PDF
  that 404s or times out is a capture loss, so the run is not a clean
  success even when the other items were captured.
- Pass the item's URL to ``attempt(url=...)`` (flat loops) or to
  ``failed(..., url=...)`` / ``blocked(..., url=...)`` (nested loops), so the
  sample names it.  Without one, an httpx error's request URL is used.  Pass
  a redacted URL when the real one carries a session id or token.
- Route the item's network call through :meth:`FetchTally.retry` (any
  callable) or :meth:`FetchTally.http_get` (an httpx GET that must return
  2xx).  A transient error (see :func:`is_transient_fetch_error`) is retried
  once in the run after a short delay; an item that then succeeds is counted
  in ``n_recovered``, not as a failure.  Permanent errors (404, 410, other
  4xx, parse errors) are not retried and are recorded with their reason.
  After three items have failed even after their retry, the tally stops
  retrying: the site is down and the whole-run policy takes over.  Loops
  with their own anti-bot handling (the SD portal's circuit breaker) do not
  retry per item.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sized
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
import structlog

from .base import AllFetchesFailed

logger = structlog.get_logger(__name__)

_MAX_ERROR_LEN = 300

#: Per-item samples kept on each tally (and in ``scraper_runs.error_details``).
#: The counts are always exact; only the list of examples is capped.
MAX_FETCH_FAILURE_SAMPLES = 50

#: In-run retry of one item's transient fetch error (#5019): two attempts in
#: all, a short pause between them.
ITEM_RETRY_ATTEMPTS = 2
ITEM_RETRY_DELAY_SECONDS = 2.0
#: Items that may fail even after their retry before the tally stops
#: retrying.  Past that the site is down; retrying every remaining item
#: would only stretch an outage run.
MAX_EXHAUSTED_ITEM_RETRIES = 3

#: HTTP statuses that mean the item is gone, not that the server is busy.
_PERMANENT_HTTP_STATUS = frozenset({404, 410})

# The tallies of the fetch now running, set by BaseScraper (#5019).
_run_tallies: ContextVar[list[FetchTally] | None] = ContextVar("fetch_tally_run", default=None)


@contextmanager
def collect_fetch_tallies() -> Iterator[list[FetchTally]]:
    """Collect every :class:`FetchTally` created inside the block (#5019).

    ``BaseScraper._fetch_with_retry`` wraps each ``fetch_documents`` call in
    this, so a scraper's per-item failures reach ``scraper_runs`` without the
    scraper passing its tally anywhere.  A context variable carries the list,
    so it also reaches the coroutine an async scraper runs with
    ``asyncio.run``.
    """
    tallies: list[FetchTally] = []
    token = _run_tallies.set(tallies)
    try:
        yield tallies
    finally:
        _run_tallies.reset(token)


# 5xx answers that mean "the server is struggling right now".  429 is left
# out on purpose: retrying a rate limit seconds later makes it worse.
_TRANSIENT_HTTP_STATUS = frozenset({500, 502, 503, 504})

_TRANSIENT_HTTPX_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


def _is_transient_one(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _TRANSIENT_HTTP_STATUS
    if isinstance(error, _TRANSIENT_HTTPX_ERRORS):
        return True
    # Builtin TimeoutError also covers asyncio.TimeoutError (3.11+).
    if isinstance(error, TimeoutError | ConnectionError):
        return True
    # Playwright is optional here, so match its errors by module and name.
    cls = type(error)
    if cls.__module__.startswith("playwright"):
        if cls.__name__ == "TimeoutError":
            return True
        # Chromium network failures: net::ERR_CONNECTION_RESET, ERR_TIMED_OUT...
        return "net::ERR_" in str(error)
    return False


def is_transient_fetch_error(error: BaseException) -> bool:
    """True when *error* looks like a network blip a retry might clear (#4713).

    Transient: timeouts, connection and proxy errors, dropped connections,
    HTTP 500/502/503/504, Playwright timeouts and ``net::ERR_*`` failures.
    Everything else, including HTTP 4xx (403 block, 404 moved page, 429
    rate limit) and parse errors from a layout change, is deterministic:
    rerunning seconds later gets the same answer.  An error raised ``from``
    a transient one counts as transient.
    """
    if _is_transient_one(error):
        return True
    cause = error.__cause__
    return cause is not None and _is_transient_one(cause)


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


def _error_url(error: BaseException | str) -> str | None:
    """The request URL an httpx error carries, or None."""
    if not isinstance(error, httpx.HTTPError):
        return None
    try:
        return str(error.request.url)
    except RuntimeError:  # an httpx error built without a request
        return None


def _http_status(error: BaseException | str) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def _request_ok(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    response = getattr(client, method)(url, **kwargs)
    response.raise_for_status()
    return response


class FetchTally:
    """Count per-item fetch outcomes and fail the run when none succeeded."""

    def __init__(self, what: str = "fetches") -> None:
        # Plural noun phrase used in the failure message, e.g. "PDF fetches".
        self.what = what
        self.n_attempted = 0
        self.n_failed = 0
        self.n_blocked = 0
        # Failures / blocks a whole-run retry might clear (#4713).
        self.n_transient_failed = 0
        self.n_transient_blocked = 0
        self.last_error: str | None = None
        self.last_block_reason: str | None = None
        # Set by abort(): why the loop stopped and how many items it skipped.
        self.abort_reason: str | None = None
        self.n_skipped = 0
        # In-run per-item retries (#5019): retry attempts made, and items
        # that succeeded on a retry.
        self.n_retries = 0
        self.n_recovered = 0
        self._n_exhausted_retries = 0
        # One dict per failed / blocked item, capped (#5019).
        self.samples: list[dict[str, Any]] = []
        self._open = False
        self._open_url: str | None = None
        tallies = _run_tallies.get()
        if tallies is not None:
            tallies.append(self)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def attempt(self, url: str | None = None) -> None:
        """Start a new item attempt. Unmarked attempts count as succeeded.

        *url* names the item in a failure sample if the attempt later fails
        or is blocked (#5019).
        """
        self.n_attempted += 1
        self._open = True
        self._open_url = url

    def _close_or_count(self) -> str | None:
        """Close the open attempt (or count a new one); return its URL."""
        url = self._open_url
        self._open_url = None
        if self._open:
            self._open = False
        else:
            self.n_attempted += 1
        return url

    def ok(self) -> None:
        """Record an explicit success."""
        self._close_or_count()

    def failed(self, error: BaseException | str, *, url: str | None = None) -> None:
        """Mark the current attempt as failed with *error*.

        *url* overrides the URL given to :meth:`attempt`; with neither, an
        httpx error's request URL is recorded.
        """
        open_url = self._close_or_count()
        self.n_failed += 1
        transient = isinstance(error, BaseException) and is_transient_fetch_error(error)
        if transient:
            self.n_transient_failed += 1
        self.last_error = _describe(error)
        status = _http_status(error)
        self._sample(
            "failed",
            url=url or open_url or _error_url(error),
            error=self.last_error,
            transient=transient,
            http_status=status,
            permanent=status in _PERMANENT_HTTP_STATUS,
        )

    def blocked(self, reason: str, *, transient: bool = False, url: str | None = None) -> None:
        """Mark the current attempt as blocked (anti-bot page, expired session).

        Pass ``transient=True`` only for a block a fresh run clears by
        construction, such as an expired session or stale ViewState that
        ``fetch_documents`` re-acquires at its start.  Anti-bot and
        access-denied pages, and responses of the wrong shape, stay
        deterministic: a retry seconds later gets the same page (#4713).
        """
        open_url = self._close_or_count()
        self.n_blocked += 1
        if transient:
            self.n_transient_blocked += 1
        self.last_block_reason = _describe(reason)
        self._sample(
            "blocked",
            url=url or open_url,
            error=self.last_block_reason,
            transient=transient,
            http_status=None,
            permanent=False,
        )

    def _sample(self, outcome: str, **fields: Any) -> None:
        if len(self.samples) < MAX_FETCH_FAILURE_SAMPLES:
            self.samples.append({"what": self.what, "outcome": outcome, **fields})

    # ------------------------------------------------------------------
    # In-run per-item retry (#5019)
    # ------------------------------------------------------------------

    def retry[T](self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Call ``fn(*args, **kwargs)``, retrying a transient error once.

        Only errors :func:`is_transient_fetch_error` accepts are retried
        (timeouts, connection resets, 5xx); a 404, another 4xx or a parse
        error raises at once.  The final error propagates, so the caller's
        ``except`` still records it with :meth:`failed`.  Wrap only the
        network call, never the parse of its result, and only calls that
        are safe to repeat (GETs, form POSTs that only read).
        """
        attempt = 1
        while True:
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if attempt >= ITEM_RETRY_ATTEMPTS or not is_transient_fetch_error(exc):
                    if attempt > 1:
                        self._n_exhausted_retries += 1
                    raise
                if self._n_exhausted_retries >= MAX_EXHAUSTED_ITEM_RETRIES:
                    raise
                self.n_retries += 1
                logger.warning(
                    "fetch.item_retrying",
                    what=self.what,
                    url=self._open_url or _error_url(exc),
                    attempt=attempt,
                    wait_seconds=ITEM_RETRY_DELAY_SECONDS,
                    error=_describe(exc),
                )
                time.sleep(ITEM_RETRY_DELAY_SECONDS)
                attempt += 1
                continue
            if attempt > 1:
                self.n_recovered += 1
            return result

    def http_get(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """GET *url* with :meth:`retry`; raise for a non-2xx status."""
        return self.retry(_request_ok, client, "get", url, **kwargs)

    def http_post(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """POST *url* with :meth:`retry`; raise for a non-2xx status.

        Only for POSTs that read (a search form), never ones that change
        state on the court's side.
        """
        return self.retry(_request_ok, client, "post", url, **kwargs)

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

    def no_retry_reason(self) -> str | None:
        """Why a whole-run retry would repeat this failure, or None (#4713).

        None means every failure looked transient, so ``BaseScraper.run()``
        may run the fetch once more.
        """
        if self.aborted:
            return "the loop aborted early (circuit breaker); rerunning would hit the same streak"
        hard_blocked = self.n_blocked - self.n_transient_blocked
        if hard_blocked:
            return (
                f"{hard_blocked} of {self.n_attempted} {self.what} were blocked; "
                "block and access-denied pages do not clear in seconds"
            )
        hard_failed = self.n_failed - self.n_transient_failed
        if hard_failed:
            return (
                f"{hard_failed} of {self.n_attempted} {self.what} failed with "
                "errors that are not transient (HTTP 4xx, parse error, or unknown)"
            )
        return None

    def log_fields(self) -> dict[str, Any]:
        """Counts as structlog keyword arguments."""
        return {
            "attempted": self.n_attempted,
            "succeeded": self.n_ok,
            "blocked": self.n_blocked,
            "failed": self.n_failed,
            "skipped": self.n_skipped,
            "retries": self.n_retries,
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
        """Raise :class:`AllFetchesFailed` if nothing was captured and either
        every attempt failed or the loop aborted with items left.

        *message* replaces the default :meth:`failure_message` when a scraper
        has a more specific diagnosis (e.g. SD's anti-bot summary). It applies
        only to the all-failed case; an abort after some successes always
        reports :meth:`abort_message`.

        The exception carries :meth:`no_retry_reason`, which
        ``BaseScraper.run()`` uses to decide on a whole-run retry (#4713).
        """
        if len(docs) != 0:
            return
        if self.all_failed:
            text = message or self.failure_message()
            if self.aborted:
                text += f"; {self.n_skipped} more skipped after abort"
            raise AllFetchesFailed(text, no_retry_reason=self.no_retry_reason())
        if self.aborted:
            raise AllFetchesFailed(self.abort_message(), no_retry_reason=self.no_retry_reason())
