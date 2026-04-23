# Correctly-labeled S3 orphans — root cause investigation (2026-04)

**Issue:** #2662 (investigate and recover correctly-labeled S3 orphans — SC + OC)
**Related:** #2638 (mislabeled S3 writes — separate orphan class, closed)
**Status:** Root cause identified via code analysis. Actual counts require running the
audit script against dev. Recovery path confirmed (rebuild_db.py).

## TL;DR

A second class of S3 orphans was identified during the #2638 investigation: objects
whose filename SHA-256 correctly matches the object bytes but whose S3 key has no
corresponding `derived.documents` row. These are **correctly-labeled orphans** —
the document reached S3 but was never ingested into the database.

Code analysis (without runtime data) points to **three credible root causes**, ranked
by likelihood:

1. **Redis Stream MAXLEN trim** — `EventBus.emit_document_captured` publishes to
   `document.captured` with `maxlen=10_000, approximate=True`. If the ingestion
   worker is slow or paused, the stream fills and older events are trimmed before
   they are consumed. S3 archival already completed at that point; the event is
   silently dropped. **Most likely cause** — matches the clustering pattern
   expected (bursts of captures during a scraper spike exhaust the backlog).

2. **Worker exhausted-retry dead-letter (XACK-and-drop)** — `worker.py:3090` XACKs
   a message after `max_retries` failed attempts. If the event had a transient
   processing error (e.g. LLM timeout, DB connectivity blip) that resolved itself,
   the document is acknowledged out of the PEL and never retried. The S3 object
   exists; no DB row was written.

3. **S3-put / event-emit race** — `_process_document` in `base.py` runs
   `archiver.archive(doc)` then immediately calls `event_bus.emit_document_captured`.
   These are sequential (not atomic). A process crash, ECS task eviction, or OOM
   kill between the two calls leaves the object in S3 with no event emitted. This
   would appear as an orphan with a `LastModified` timestamp that predates the
   worker's startup.

Actual counts require running the audit script against dev S3. See §Census.

## Census

> **Note:** The counts below are placeholders. Run the audit script to populate
> actual values.

```
scripts/ecs-run-task.sh scripts/audit_correctly_labeled_s3_orphans.py \
    -- --county both --json > tmp/orphan-census.json
```

Expected output shape:
```json
{
  "all_clean": false,
  "counties": {
    "santa_clara": {
      "total": <N>,
      "in_db": <N>,
      "orphans": <N>,
      "mislabeled": <N>,
      "orphans_by_month": {"2026-03": <N>, "2026-04": <N>},
      "sample_verified": [...]
    },
    "orange": {
      "total": <N>,
      "in_db": <N>,
      "orphans": <N>,
      "mislabeled": <N>,
      "orphans_by_month": {},
      "sample_verified": [...]
    }
  }
}
```

The `orphans_by_month` histogram is the key diagnostic signal:

- **Spike concentrated around 2026-03-28** → likely migration-window confusion
  (mislabeled orphans misclassified — recheck with `--sample-head-objects 50`).
- **Spike around a scraper deployment date** → MAXLEN eviction during scraper restart
  bursts most likely.
- **Distributed low-level drip across months** → ECS task evictions (S3/event race)
  or retry-exhaustion (XACK-and-drop) most likely.

## Representative orphan

> **Note:** This section requires a concrete orphan key from the audit run.
> Placeholder forensic walkthrough follows — replace with actual key after running.

### Forensic walk (template)

```bash
# 1. Pick a representative orphan key from the census output.
ORPHAN_KEY="ca/santa_clara/superior_court/raw/<sha256>.pdf"

# 2. HEAD the object to extract capture-timestamp and scraper-id metadata.
aws s3api head-object \
    --bucket judgemind-document-archive-dev \
    --key "$ORPHAN_KEY"
# Expected output:
#   LastModified: <timestamp>
#   Metadata:
#     capture-timestamp: <ISO datetime>
#     scraper-id: ca-sc-tentatives-civil
#     content-hash: <sha256>  ← should match filename

# 3. Correlate with CloudWatch logs using capture-timestamp ± 60 seconds.
#    Log groups:
#      /ecs/judgemind-scraper-dev       ← scraper side: confirms S3 put succeeded
#      /ecs/judgemind-ingestion-worker-dev ← worker side: confirms event processing

# Scraper log (confirms archive() call succeeded):
#   INFO  Archived 142080 bytes to s3://judgemind-document-archive-dev/<key>

# Worker log (if event was received):
#   INFO  Processing document.captured msg_id=<redis-msg-id>

# If the scraper log shows the archive call but the worker log shows no corresponding
# processing event around the same timestamp → event was lost in the stream (MAXLEN
# eviction or S3/event race between the two log lines).
```

### Expected finding (based on code analysis)

Given the `S3Archiver.archive()` implementation writes the `capture-timestamp` metadata
at PutObject time and `EventBus.emit_document_captured` is called immediately after in
`_process_document`, any orphan with:

- A `capture-timestamp` metadata that predates the ingestion worker's last start time
- No corresponding `document.captured` stream entry in the worker logs

...is consistent with the **MAXLEN eviction** hypothesis: the event was emitted into the
stream but trimmed before the consumer group acknowledged it.

Any orphan with a `capture-timestamp` that matches a known ECS task eviction event in
CloudWatch Container Insights is consistent with the **S3-put/event-emit race**.

## Root cause

**Primary (most likely): Redis Stream MAXLEN eviction**

`EventBus.emit_document_captured` (events.py line 75–81) uses:
```python
self._redis.xadd(
    STREAM_DOCUMENT_CAPTURED,
    {"data": json.dumps(payload)},
    maxlen=self._maxlen,       # default 10_000
    approximate=True,          # allows Redis to trim lazily
)
```

The `approximate=True` flag instructs Redis to trim the stream when it exceeds
`maxlen=10_000` but allows overage of up to ~10% before trimming. During scraper
burst runs (e.g. a scraper re-run after a downtime window captures 200–300 documents
rapidly), the stream can fill faster than the ingestion worker consumes it. When Redis
trims the oldest entries, any un-ACKed entries that have been trimmed from the stream
body but remain in the PEL are handled in `worker.py:2988–2993`:

```python
for msg_id, data in claimed_messages:
    if data is None:
        # Message was deleted from the stream but still in PEL.
        # Acknowledge it to clear the PEL entry.
        logger.debug("Acknowledging deleted PEL entry %s", msg_id)
        self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
        continue
```

This path correctly acknowledges and discards entries that were trimmed from the stream
body — but those entries represent documents that were archived to S3 and never ingested.
The resulting orphan is correctly labeled (the scraper wrote the right hash to both the
S3 key and the object metadata), but has no `derived.documents` row.

**Secondary (also credible): Worker exhausted-retry XACK-and-drop**

`worker.py:3084–3090` dead-letters after `max_retries` failed attempts:
```python
logger.critical(
    "Dead-lettering message %s after %d retries. Last error: %s",
    msg_id, self._max_retries, last_exc,
)
self._redis.xack(STREAM_DOCUMENT_CAPTURED, CONSUMER_GROUP, msg_id)
```

This path is guarded by `is_infrastructure_error` (which re-raises for restart) and
`is_schema_constraint_error` (which dead-letters immediately), so it should only fire
for transient application errors that eventually stabilize. If a document's LLM
extraction fails consistently due to a model API timeout, the document is discarded.
The S3 object exists; no DB row is written.

**Tertiary: S3-put / event-emit race**

`_process_document` in `base.py` (lines 203–209) runs archive then emit sequentially:
```python
if self._archiver:
    doc.s3_key = self._archiver.archive(doc)
    doc.s3_bucket = self._archiver.bucket
    doc.validation_status = ValidationStatus.PENDING

if self._event_bus:
    self._event_bus.emit_document_captured(doc, producer_id=self.config.scraper_id)
```

A process kill between these two lines (ECS task stopped for deployment, OOM, spot
interruption) leaves the S3 object in place with no event emitted. This class would
cluster at known ECS deployment timestamps.

## Recovery

Correctly-labeled orphans are fully recoverable via `rebuild_db.py` because the
rebuild pipeline discovers documents from S3, not from the stream. Run:

```bash
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county santa_clara
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county orange
```

**Pre/post verification** — re-run the audit before and after rebuild to confirm
the orphan count drops to zero:

```bash
# Before:
scripts/ecs-run-task.sh scripts/audit_correctly_labeled_s3_orphans.py \
    -- --county both --json > tmp/orphan-pre-rebuild.json

# Run rebuild:
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county santa_clara
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county orange

# After:
scripts/ecs-run-task.sh scripts/audit_correctly_labeled_s3_orphans.py \
    -- --county both --json > tmp/orphan-post-rebuild.json

# Delta:
python3 -c "
import json
pre = json.load(open('tmp/orphan-pre-rebuild.json'))
post = json.load(open('tmp/orphan-post-rebuild.json'))
for county in pre['counties']:
    before = pre['counties'][county]['orphans']
    after = post['counties'][county]['orphans']
    print(f'{county}: {before} orphans → {after} orphans')
"
```

Expected output (placeholder — replace with actual numbers after running):
```
santa_clara: <N> orphans → 0 orphans
orange: <N> orphans → 0 orphans
```

Note: `rebuild_db.py` is idempotent — it uses `INSERT ... ON CONFLICT DO NOTHING`
on `documents.id` (which is derived from `content_hash`), so re-running against
already-ingested documents is safe.

## Follow-ups

### AC3 — preventing this class going forward

The root cause analysis identifies a structural gap: S3 archival and stream event
emission are not atomic. Recovery is always possible via rebuild, but the class
of orphan can be minimized by:

1. **Raise the MAXLEN** — increasing `DEFAULT_STREAM_MAXLEN` from 10,000 to 50,000
   raises the burst headroom significantly. Filed as: see follow-up issue referenced
   below.

2. **Dead-letter to DLQ instead of XACK-drop** — `worker.py:3090` should persist the
   failed event payload to a dead-letter queue (e.g. a Redis `document.captured.dlq`
   stream or an SQS queue) before XACKing, enabling operator review and replay.

3. **Transactional outbox** — wrap S3 archival + event emission in a write-ahead log
   so process crashes don't produce silent orphans. Higher engineering lift; tracked
   separately.

**Filed follow-up issues:**

- **#3096** — `fix(ingestion): raise DEFAULT_STREAM_MAXLEN to reduce correctly-labeled orphan rate`
  (raises 10,000 → 50,000; within cache.t4g.micro budget)
- The XACK-and-drop DLQ pattern and transactional outbox are out of scope for this
  PR per the scope boundary in #2662. They are tracked as separate issues.
