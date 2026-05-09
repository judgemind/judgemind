#!/usr/bin/env python3
"""Helper for sweep-completed-parents.sh — auto-close meta-tasks (#4499).

Not meant to be run directly. Called by ``sweep-completed-parents.sh`` with
environment variables: REPO, DRY_RUN, GRACE_HOURS, CHILDREN_FILE,
WORK_TMPDIR, SCRIPT_DIR.

Algorithm
---------
1. Read ``CHILDREN_FILE`` — a JSON array of {number, state, closedAt, body}
   produced by ``gh issue list --search 'in:body "Parent: #"' --state all``.
2. For each child, parse the canonical ``Parent: #<N>`` line out of its
   body via :func:`scripts.dispatcher.parent_issue.parse_parent_issue`
   (shared single source of truth — see #4508).
3. Group children by parent number. The PARENT SET is the unique set of
   parent numbers extracted in step 2.
4. For each candidate parent:
   a. Fetch the parent's state, closedAt, and updatedAt via ``gh issue
      view`` (one ``--json`` call). Skip if the parent is already CLOSED.
   b. Verify ALL children of this parent (from step 3's grouping) are
      CLOSED. Skip if any are OPEN.
   c. Verify no child was closed via ``--reason not_planned``. We query
      via ``gh issue view --json stateReason`` per child the first time we
      see one closed without a confirmed reason. (Rare; cached.)
   d. Compute the most-recent ``closedAt`` across all children. Require
      it to be ``>= grace_hours`` ago. Skip otherwise.
   e. Fetch parent comments newer than the most-recent child closedAt.
      Skip if any non-bot comments exist (suggests human follow-up).
   f. Otherwise: post the auto-close comment, ``gh issue close --reason
      completed``, and ``scripts/unblock-dependents.sh <parent>``.

Run via ``scripts/sweep-completed-parents.sh`` (the bash front end populates
the env vars and the children JSON file). The helper is intentionally
side-effect-free until step (f) — every short-circuit short-skip is
visible in the ``[skip]`` log lines so the dry-run output reads as a
candidate list without action.
"""

# permanent: true
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the canonical Parent: #N regex helper importable when this script
# runs directly (``python3 scripts/_sweep_completed_parents.py``). The
# helper lives at ``scripts/dispatcher/parent_issue.py`` and is the
# single source of truth — see #4508. CI's pytest run already has
# ``PYTHONPATH=scripts`` so the import would resolve there too, but the
# direct-invocation path needs an explicit insert.
_DISPATCHER_DIR = Path(__file__).resolve().parent / "dispatcher"
if str(_DISPATCHER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR.parent))

from dispatcher.parent_issue import parse_parent_issue  # noqa: E402

# Bot-author logins whose comments are filtered out of the
# "comments-since-last-child-closed" check. A trailing ``[bot]`` segment is
# also recognized via :func:`_is_bot_author` so any future bot integrations
# inherit the skip without code change.
_BOT_AUTHORS = frozenset(
    {
        "github-actions",
        "judgemind-agent",
        "judgeminder",
    }
)

# Auto-close comment template — names the children, the date, and the
# script that closed it for audit-trail purposes. The wording mirrors what
# the issue #4499 body proposes verbatim so future readers have an obvious
# breadcrumb back to the design.
_AUTOCLOSE_TEMPLATE = (
    "Auto-closing as completed — all sub-tasks ({child_list}) closed on "
    "or before {last_close_date}. If the parent's acceptance criteria "
    "were not fully met by the sub-tasks, please reopen and link the "
    "residual follow-up.\n\n"
    "_Closed by `scripts/sweep-completed-parents.sh` (#4499). "
    "Grace window: {grace_hours}h._"
)


@dataclass(frozen=True)
class _Child:
    """Single sub-task entry parsed from the children-list JSON."""

    number: int
    state: str  # OPEN / CLOSED
    closed_at: datetime | None  # Always present when state == CLOSED
    state_reason: str | None  # Filled lazily — populated by _state_reason_for


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse a GitHub-formatted ISO 8601 timestamp into a UTC datetime."""
    if not value:
        return None
    # GitHub returns 'Z' for UTC; Python <3.11 doesn't accept it directly.
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_bot_author(login: str | None) -> bool:
    """Return True if ``login`` looks like a bot account.

    Recognises explicit names in ``_BOT_AUTHORS`` plus any GitHub App login
    ending in ``[bot]`` (the suffix GitHub stamps onto bot logins).
    """
    if not login:
        return False
    if login in _BOT_AUTHORS:
        return True
    return login.endswith("[bot]")


def parse_parent_number(body: str) -> int | None:
    """Backwards-compatible alias for :func:`parse_parent_issue`.

    The legacy name is retained because :mod:`scripts.tests.test_sweep_completed_parents`
    imports it. The implementation lives in
    :mod:`scripts.dispatcher.parent_issue` — see #4508.
    """
    return parse_parent_issue(body)


def group_children_by_parent(
    children: list[dict],
) -> dict[int, list[_Child]]:
    """Group raw children-list JSON entries by their parent issue number."""
    grouped: dict[int, list[_Child]] = defaultdict(list)
    for entry in children:
        body = entry.get("body") or ""
        parent = parse_parent_issue(body)
        if parent is None:
            continue
        grouped[parent].append(
            _Child(
                number=int(entry["number"]),
                state=str(entry.get("state", "UNKNOWN")).upper(),
                closed_at=_parse_iso8601(entry.get("closedAt")),
                state_reason=None,
            )
        )
    return dict(grouped)


def _gh_json(*args: str) -> dict | list | None:
    """Run ``gh`` and parse stdout as JSON; return None on error."""
    try:
        r = subprocess.run(  # noqa: S603 — args are well-typed
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        print("  ERROR: gh CLI not found", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"  gh error: {r.stderr.strip()[:200]}", file=sys.stderr)
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(f"  gh JSON parse failure: {out[:200]}", file=sys.stderr)
        return None


def _gh_run(*args: str) -> tuple[int, str, str]:
    """Run a non-JSON ``gh`` command (e.g. close, edit, comment)."""
    try:
        r = subprocess.run(  # noqa: S603 — args are well-typed
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "gh CLI not found"
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def is_parent_closeable(
    parent_state: str,
    children: list[_Child],
    *,
    now: datetime,
    grace: timedelta,
) -> tuple[bool, str]:
    """Return (closeable, reason) for one parent + its children.

    Pure function — no I/O. Used directly by the integration code below
    AND by the unit tests in ``scripts/tests/test_sweep_completed_parents.py``
    so the gating logic is verifiable without mocking ``gh``.

    Closeable iff:
      * Parent is OPEN.
      * At least one child exists.
      * Every child is CLOSED.
      * No child has ``state_reason == "not_planned"``.
      * Most-recent child ``closed_at`` is older than ``grace``.
    """
    if parent_state.upper() != "OPEN":
        return False, f"parent is {parent_state}"
    if not children:
        return False, "no children grouped under this parent"
    open_children = [c.number for c in children if c.state != "CLOSED"]
    if open_children:
        return False, f"open children: {open_children}"
    not_planned = [c.number for c in children if c.state_reason == "not_planned"]
    if not_planned:
        return False, f"closed-not-planned children: {not_planned}"
    closed_ats = [c.closed_at for c in children if c.closed_at is not None]
    if not closed_ats:
        return False, "no children carry closedAt timestamps"
    most_recent = max(closed_ats)
    age = now - most_recent
    if age < grace:
        return False, (
            f"most-recent child closed {age.total_seconds() / 3600.0:.1f}h ago "
            f"(< grace {grace.total_seconds() / 3600.0:.1f}h)"
        )
    return True, "all children closed; outside grace window"


def has_human_comments_since(
    comments: list[dict],
    cutoff: datetime,
) -> tuple[bool, list[int]]:
    """Return (has_human, comment_count_after_cutoff).

    A "human" comment is any comment authored by a non-bot login (see
    :func:`_is_bot_author`) and posted strictly after ``cutoff``.
    """
    after = [
        c
        for c in comments
        if (created := _parse_iso8601(c.get("createdAt"))) is not None
        and created > cutoff
    ]
    human_after = [
        c for c in after if not _is_bot_author((c.get("author") or {}).get("login"))
    ]
    return bool(human_after), [c.get("id") for c in after]


def _fetch_parent_info(repo: str, parent: int) -> dict | None:
    """Fetch parent issue state, comments, closedAt via one ``gh`` call."""
    return _gh_json(
        "issue",
        "view",
        str(parent),
        "--repo",
        repo,
        "--json",
        "number,state,closedAt,updatedAt,comments,labels",
    )


def _resolve_state_reason(repo: str, child: _Child) -> _Child:
    """Populate ``state_reason`` for a CLOSED child (one extra ``gh`` call)."""
    if child.state != "CLOSED" or child.state_reason is not None:
        return child
    info = _gh_json(
        "issue",
        "view",
        str(child.number),
        "--repo",
        repo,
        "--json",
        "stateReason",
    )
    reason: str | None = None
    if isinstance(info, dict):
        # gh's stateReason field is one of: COMPLETED, NOT_PLANNED, REOPENED, null.
        # Normalise to lower-case for the comparison in is_parent_closeable.
        raw = info.get("stateReason")
        if isinstance(raw, str):
            reason = raw.lower()
    return _Child(
        number=child.number,
        state=child.state,
        closed_at=child.closed_at,
        state_reason=reason,
    )


def _build_autoclose_comment(
    children: list[_Child],
    grace_hours: int,
) -> str:
    """Render the auto-close comment body."""
    sorted_children = sorted(children, key=lambda c: c.number)
    child_list = ", ".join(f"#{c.number}" for c in sorted_children)
    closed_ats = [c.closed_at for c in children if c.closed_at is not None]
    last = max(closed_ats) if closed_ats else datetime.now(timezone.utc)
    return _AUTOCLOSE_TEMPLATE.format(
        child_list=child_list,
        last_close_date=last.strftime("%Y-%m-%d"),
        grace_hours=grace_hours,
    )


def _close_parent(
    repo: str,
    parent: int,
    children: list[_Child],
    grace_hours: int,
    tmpdir: str,
    script_dir: str,
) -> bool:
    """Post comment, close parent, run unblock-dependents.sh. Return True on success."""
    comment_path = os.path.join(tmpdir, f"_sweep_autoclose_{parent}.txt")
    body = _build_autoclose_comment(children, grace_hours)
    with open(comment_path, "w") as f:
        f.write(body + "\n")

    # 1. Comment.
    rc, _out, err = _gh_run(
        "issue",
        "comment",
        str(parent),
        "--repo",
        repo,
        "--body-file",
        comment_path,
    )
    if rc != 0:
        print(f"  ERROR posting comment on #{parent}: {err[:200]}", file=sys.stderr)
        return False

    # 2. Close.
    rc, _out, err = _gh_run(
        "issue",
        "close",
        str(parent),
        "--repo",
        repo,
        "--reason",
        "completed",
    )
    if rc != 0:
        print(f"  ERROR closing #{parent}: {err[:200]}", file=sys.stderr)
        return False

    # 3. Unblock dependents (best-effort; failure here is logged but does
    #    not roll back the close).
    unblock = os.path.join(script_dir, "unblock-dependents.sh")
    try:
        r = subprocess.run(  # noqa: S603 — args are well-typed
            [unblock, str(parent)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if r.returncode != 0:
            print(
                f"  WARN unblock-dependents.sh exited {r.returncode} for #{parent}: "
                f"{(r.stderr or '')[:200]}",
                file=sys.stderr,
            )
    except FileNotFoundError:
        print(
            f"  WARN unblock-dependents.sh missing at {unblock}",
            file=sys.stderr,
        )

    return True


def main() -> int:
    repo = os.environ.get("REPO", "judgemind/judgemind")
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    grace_hours = int(os.environ.get("GRACE_HOURS", "24"))
    children_file = os.environ.get("CHILDREN_FILE", "")
    tmpdir = os.environ.get("WORK_TMPDIR", ".")
    script_dir = os.environ.get("SCRIPT_DIR", os.path.dirname(__file__))

    if not children_file or not os.path.exists(children_file):
        print(
            f"Error: CHILDREN_FILE not set or missing: {children_file!r}",
            file=sys.stderr,
        )
        return 1

    with open(children_file) as f:
        try:
            children_raw: list[dict] = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"Error: malformed children JSON: {exc}", file=sys.stderr)
            return 1

    if not children_raw:
        print("No children with 'Parent: #' references found — nothing to do.")
        return 0

    grouped = group_children_by_parent(children_raw)
    print(
        f"Found {len(grouped)} candidate parent(s) across "
        f"{len(children_raw)} child issue(s)."
    )
    print()

    grace = timedelta(hours=grace_hours)
    now = datetime.now(timezone.utc)

    closed_count = 0
    skipped_count = 0
    error_count = 0

    for parent, children in sorted(grouped.items()):
        print(f"Parent #{parent} (children: {[c.number for c in children]}):")

        # Fetch parent state.
        info = _fetch_parent_info(repo, parent)
        if not isinstance(info, dict):
            print(f"  [skip] could not fetch parent #{parent} state")
            skipped_count += 1
            print()
            continue

        parent_state = str(info.get("state", "UNKNOWN")).upper()
        if parent_state != "OPEN":
            print(f"  [skip] parent state is {parent_state}")
            skipped_count += 1
            print()
            continue

        # Verify each closed child's state_reason — only resolve lazily on
        # the first child for which we lack the field.
        resolved: list[_Child] = [_resolve_state_reason(repo, c) for c in children]

        closeable, reason = is_parent_closeable(
            parent_state,
            resolved,
            now=now,
            grace=grace,
        )
        if not closeable:
            print(f"  [skip] {reason}")
            skipped_count += 1
            print()
            continue

        # Comment-since-last-child-closed check. Skip if a non-bot comment
        # exists after the most-recent child close — suggests a human is
        # working on residual follow-up and we should not auto-close.
        closed_ats = [c.closed_at for c in resolved if c.closed_at is not None]
        most_recent = max(closed_ats)
        comments = info.get("comments") or []
        has_human, ids_after = has_human_comments_since(comments, most_recent)
        if has_human:
            print(
                f"  [skip] {len(ids_after)} comment(s) since last child close "
                f"({most_recent.isoformat()}) — possible human follow-up"
            )
            skipped_count += 1
            print()
            continue

        if dry_run:
            print(
                f"  [DRY RUN] would close parent #{parent}; children all closed by "
                f"{most_recent.isoformat()}"
            )
            closed_count += 1
            print()
            continue

        ok = _close_parent(
            repo,
            parent,
            resolved,
            grace_hours,
            tmpdir,
            script_dir,
        )
        if ok:
            print(f"  closed #{parent} (auto-close completed).")
            closed_count += 1
        else:
            print(f"  ERROR — auto-close of #{parent} failed mid-flight.")
            error_count += 1
        print()

    print("---")
    if dry_run:
        print(
            f"[DRY RUN] would have closed {closed_count} parent(s); "
            f"skipped {skipped_count}; errors {error_count}."
        )
    else:
        print(
            f"Closed {closed_count} parent(s); skipped {skipped_count}; "
            f"errors {error_count}."
        )
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
