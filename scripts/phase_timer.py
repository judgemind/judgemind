"""Phase timing instrumentation for the /task agent workflow.

Tracks wall-clock duration for each phase of the agent workflow (setup,
claiming, ralph-worker, ralph-reviewer, pushing, ci-watch, etc.) and
produces a timing summary at task completion.

This module is intentionally dependency-free (stdlib only) to avoid
requiring a venv for the scripts/ directory.

Usage from agent skill files:

    # Start a phase (writes a record to phase-timings.jsonl):
    python3 scripts/phase_timer.py start <worktree> <phase>

    # End the current phase:
    python3 scripts/phase_timer.py end <worktree>

    # End current phase with per-reviewer detail (ralph reviewer phases):
    python3 scripts/phase_timer.py end <worktree> --detail '{"gemini_standard":90,"gemini_adversarial":95,"claude":120}'

    # Generate timing.json and append to task-timings.jsonl:
    python3 scripts/phase_timer.py summarize <worktree> <repo_root> <issue_number>
"""
# permanent: true

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _timings_path(worktree: str | Path) -> Path:
    """Return the path to the phase timings JSONL file."""
    return Path(worktree) / "tmp" / "phase-timings.jsonl"


def _append_record(worktree: str | Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to the phase timings log."""
    path = _timings_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _read_records(worktree: str | Path) -> list[dict[str, Any]]:
    """Read all records from the phase timings log."""
    path = _timings_path(worktree)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def start_phase(worktree: str | Path, phase: str) -> None:
    """Record the start of a new phase.

    If a previous phase is still open (has start but no end), this
    automatically closes it first.

    Args:
        worktree: Path to the worktree directory.
        phase: Phase name (e.g. "setup", "ralph-worker (1)", "ci-watch (1)").
    """
    now = datetime.now(timezone.utc).isoformat()

    # Auto-close any open phase
    records = _read_records(worktree)
    for record in reversed(records):
        if record.get("type") == "start" and not _find_end(records, record):
            end_phase(worktree)
            break

    _append_record(
        worktree,
        {
            "type": "start",
            "phase": phase,
            "timestamp": now,
        },
    )


def _find_end(
    records: list[dict[str, Any]], start_record: dict[str, Any]
) -> dict[str, Any] | None:
    """Find the end record matching a start record."""
    start_phase_name = start_record.get("phase", "")
    start_ts = start_record.get("timestamp", "")
    for record in records:
        if (
            record.get("type") == "end"
            and record.get("phase") == start_phase_name
            and record.get("start_timestamp") == start_ts
        ):
            return record
    return None


def end_phase(
    worktree: str | Path,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record the end of the current (most recent open) phase.

    Args:
        worktree: Path to the worktree directory.
        detail: Optional per-sub-phase detail dict (e.g. per-reviewer timing
                for ralph-reviewer phases).
    """
    now = datetime.now(timezone.utc).isoformat()
    records = _read_records(worktree)

    # Find the most recent start without a matching end
    open_start: dict[str, Any] | None = None
    for record in reversed(records):
        if record.get("type") == "start" and not _find_end(records, record):
            open_start = record
            break

    if open_start is None:
        # No open phase to close — this is a no-op
        return

    phase = open_start.get("phase", "unknown")
    start_ts = open_start.get("timestamp", "")

    # Compute duration in seconds
    seconds = 0
    try:
        start_dt = datetime.fromisoformat(start_ts)
        end_dt = datetime.fromisoformat(now)
        seconds = int((end_dt - start_dt).total_seconds())
    except (ValueError, TypeError):
        pass

    end_record: dict[str, Any] = {
        "type": "end",
        "phase": phase,
        "start_timestamp": start_ts,
        "end_timestamp": now,
        "seconds": seconds,
    }

    if detail:
        end_record["detail"] = detail

    _append_record(worktree, end_record)


def generate_summary(
    worktree: str | Path,
    repo_root: str | Path,
    issue_number: int,
) -> dict[str, Any]:
    """Generate a timing summary from the phase timings log.

    Writes {worktree}/tmp/timing.json and appends a one-line summary to
    {repo_root}/tmp/task-timings.jsonl.

    Args:
        worktree: Path to the worktree directory.
        repo_root: Path to the main repo root (for the aggregate JSONL file).
        issue_number: GitHub issue number for the summary.

    Returns:
        The summary dict that was written.
    """
    records = _read_records(worktree)
    worktree_path = Path(worktree)
    repo_root_path = Path(repo_root)

    # Close any still-open phase
    for record in reversed(records):
        if record.get("type") == "start" and not _find_end(records, record):
            end_phase(worktree)
            # Re-read after closing
            records = _read_records(worktree)
            break

    # Build phase list from end records (they have the computed seconds)
    phases: list[dict[str, Any]] = []
    total_seconds = 0

    for record in records:
        if record.get("type") != "end":
            continue
        phase_entry: dict[str, Any] = {
            "phase": record["phase"],
            "seconds": record.get("seconds", 0),
        }
        if "detail" in record:
            phase_entry["detail"] = record["detail"]
        phases.append(phase_entry)
        total_seconds += record.get("seconds", 0)

    summary: dict[str, Any] = {
        "issue": issue_number,
        "total_seconds": total_seconds,
        "phases": phases,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Write timing.json
    timing_path = worktree_path / "tmp" / "timing.json"
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    timing_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # Append to aggregate JSONL
    aggregate_path = repo_root_path / "tmp" / "task-timings.jsonl"
    aggregate_path.parent.mkdir(parents=True, exist_ok=True)
    with aggregate_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, default=str) + "\n")

    return summary


def main() -> None:
    """CLI entry point for phase timing operations."""
    parser = argparse.ArgumentParser(
        description="Phase timing instrumentation for the /task agent workflow.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start command
    start_parser = subparsers.add_parser("start", help="Start a new phase")
    start_parser.add_argument("worktree", help="Path to the worktree directory")
    start_parser.add_argument("phase", help="Phase name")

    # end command
    end_parser = subparsers.add_parser("end", help="End the current phase")
    end_parser.add_argument("worktree", help="Path to the worktree directory")
    end_parser.add_argument(
        "--detail",
        type=str,
        default=None,
        help="JSON string with per-sub-phase detail (e.g. per-reviewer timing)",
    )

    # summarize command
    summarize_parser = subparsers.add_parser(
        "summarize", help="Generate timing summary"
    )
    summarize_parser.add_argument("worktree", help="Path to the worktree directory")
    summarize_parser.add_argument("repo_root", help="Path to the main repo root")
    summarize_parser.add_argument("issue_number", type=int, help="GitHub issue number")

    args = parser.parse_args()

    if args.command == "start":
        start_phase(args.worktree, args.phase)
    elif args.command == "end":
        detail = None
        if args.detail:
            try:
                detail = json.loads(args.detail)
            except json.JSONDecodeError:
                print(
                    f"Error: --detail must be valid JSON: {args.detail}",
                    file=sys.stderr,
                )
                sys.exit(1)
        end_phase(args.worktree, detail=detail)
    elif args.command == "summarize":
        summary = generate_summary(args.worktree, args.repo_root, args.issue_number)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
