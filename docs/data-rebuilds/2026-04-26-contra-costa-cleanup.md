# 2026-04-26 Contra Costa Rebuild — Clear Pre-#2571 Bad-Split Rows

## Motivation

Issue #2633. After PR #2632 (fix in #2571) deployed, 268 pre-existing Contra Costa
rulings with `case_number LIKE 'UNKNOWN-%'` and `case_title IS NULL` remained in
`derived.rulings`. These were ingested before the deterministic validator tightened the
multi-case-PDF split path. The rebuild clears them by re-deriving all CC rulings from S3.

## Execution

**Command:** `scripts/ecs-run-task.sh scripts/rebuild_db.py -- --reset --county "Contra Costa"`

**ECS Task ARN:** `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/c0d964267f3c4f3c86a43ec9518df8f7`

**Log stream:** `oneshot/oneshot/c0d964267f3c4f3c86a43ec9518df8f7`
(`/ecs/judgemind-ingestion-worker-dev`)

**Date:** 2026-04-26, ~19:03–19:34 UTC

**Final log line:**
```
[info] Rebuild complete errors=0 format_counts={'pdf': 276} hash_mismatch_warnings=190
       pool_break_events=0 pool_break_keys_recovered=0 pool_break_keys_unrecovered=0
       processed=276 skipped=0 total=276
```

## Results

| Metric | Before | After |
|--------|--------|-------|
| UNKNOWN-% NULL-title CC rulings | 268 | 0 |
| Total CC rulings | ~268 (bad rows) | 301 |
| S3 raws processed | — | 231 raw PDFs |
| Documents processed (incl. splits) | — | 276 |
| Rebuild errors | — | 0 |
| Hash-mismatch warnings | — | 190 |

## Notes

- 190 hash-mismatch warnings are expected: Contra Costa uses multi-case PDFs where
  the SHA-256 of the raw doesn't match the stored hash. Since PR #2512, these are
  routed through `_llm_split_document` rather than skipped. Split-children that fail
  the deterministic validator are correctly gated and not written to `derived.rulings`.
- The spot-check PDF `406574e2...pdf` (referenced in AC #3) exists in `derived.documents`
  but its split-children failed deterministic validation — this is correct gating behavior.
  Case C24-01160/DENEA MARHX is in one of those gated PDFs.

## Related

- Issue #2633
- PR #2632 (code fix — #2571)
- PR #2512 (hash-mismatch routing fix — #2494)
- PR #3460 (IAM widening that unblocked this rebuild)
