# scripts/archive/

Completed one-off scripts that have been executed and are not expected to be
run again. They are preserved here for historical reference (git blame shows
what data changes were made and when).

## Why archive instead of delete?

These scripts document the data transformations applied to the production
database. When investigating a data issue, `git log --oneline -- scripts/archive/<name>.py`
shows when the script was created and run. The docstrings often reference
the GitHub issue that motivated the change.

## What belongs here?

Scripts that meet ALL of these criteria:

1. **One-off execution** -- backfills, cleanups, fixups, deduplication, or
   data migrations that were run once on dev/production.
2. **Not reusable** -- the script addresses a specific historical data issue,
   not a recurring operational need.
3. **Completed** -- the linked GitHub issue (if any) is closed.

## What stays in scripts/?

- Reusable tools (`reingest_from_s3.py`, `audit_field_completeness.py`, etc.)
- CI check scripts (`check-*.py`)
- Agent infrastructure (`gemini_review.py`, `phase_timer.py`, etc.)
- Generic backfills that may be needed for new counties or schema changes

## CI exclusion

The CI check scripts (`check-oneshot-imports.sh`, `check-oneshot-repo-paths.sh`)
scan only `scripts/*.py` (no recursion), so archived scripts are automatically
excluded from those checks.
