# Mislabeled S3 writes — root cause (2026-04)

**Issue:** #2638 (investigate root cause of mislabeled S3 writes producing SC flat-hash orphans)
**Parent:** #2628 (downstream fix — reingest handles mismatches gracefully)
**Status:** Root cause identified. Live scraper is NOT the cause. Historical artifact only.

## TL;DR

The mislabeled S3 objects (filename SHA-256 does not match the SHA-256 of the bytes) were produced on **2026-03-28** by the **one-time migration script** that ran for PR #2193 (commit `fbf8e38`, now archived at `scripts/archive/migrate_s3_keys.py`). The live scraper has **never** produced mislabeled writes and is not the cause. All hypotheses in the original #2638 issue (shared doc state, raw_content mutation, retry reuse) were incorrect.

The bug class is closed at the scraper: every post-migration capture examined has filename = SHA-256 of bytes. The remaining work is **cleanup** of the historical mislabeled objects, not a scraper fix.

The #2628 investigation conflated two distinct orphan classes:

1. **Mislabeled writes** (filename ≠ bytes hash) — historical artifacts from the 2026-03-28 migration bug. Bounded set, can be fixed by a targeted re-key pass.
2. **Correctly-labeled orphans** (filename = bytes hash, but no matching `derived.documents` row) — a *different* mechanism (likely Redis MAXLEN eviction, ingestion worker exception, or capture predating documents table). Not addressed by #2628 and still open — see follow-up below.

## Forensic evidence

### The migration commit

```
fbf8e38 feat(storage): content-addressed S3 keys with local cache (#2193)
Date:   Sat Mar 28 15:49:15 2026 -0700  (22:49:15 UTC)

Migration script (scripts/migrate_s3_keys.py) copies S3 objects to new keys
and updates DB rows. Migration executed on dev: 6,350 documents migrated,
0 errors.
```

### Mislabeled SC object — e3fd8c…pdf

```
$ aws s3api head-object --bucket judgemind-document-archive-dev \
    --key ca/santa_clara/superior_court/raw/e3fd8c...pdf
LastModified:  2026-03-28T22:35:29+00:00
ETag:          "..."
Metadata:
  capture-timestamp: 2026-03-<earlier>           ← original capture date
  content-hash:      4728f93b…                    ← matches bytes, NOT filename
  scraper-id:        ca-sc-tentatives-civil
```

### Mislabeled SC trio (919592f3 / 5dabea2f / 9d02fc8f)

All three are byte-identical (Dept 6, Hon. Sivilla-Jones calendar 2026-03-12, ContentLength 442105) and share `ETag "1c05da9dd7a0a068bcb79f832abb8240"`. Metadata `content-hash` on all three is `7c0433e17aca7523…` (the actual bytes hash), matching none of the filenames.

- LastModified on all three: **2026-03-28T22:36:xx UTC** (migration window).

### Mislabeled OC pair (74947278 / fbc5d7e6)

Byte-identical (Dept C24 Hon. Martinez calendar, ContentLength 180976, ETag `"7385e01f239b095a18512836042a815e"`). Metadata `content-hash` `cde28043c1bd7d38…`, matches neither filename.

- LastModified: **2026-03-28T22:32:32-33 UTC** (migration window).
- capture-timestamp metadata: `2026-03-14T13:20:16` — the *real* capture date, preserved across the migration CopyObject call.

### Post-migration capture (2026-04-16)

```
ca/santa_clara/superior_court/raw/15d4bc6b…pdf
LastModified: 2026-04-16T...
filename SHA-256: 15d4bc6b…
actual bytes SHA-256: 15d4bc6b…   ← MATCH — live scraper is correct
```

### "Orphan" 79d74acea750…pdf (2026-04-14)

Flagged in #2628 investigation as an orphan. HEAD shows:

```
LastModified: 2026-04-14T...
filename SHA-256: 79d74acea750…
actual bytes SHA-256: 79d74acea750…   ← MATCH
```

This is a **correctly-labeled** S3 object with no matching `derived.documents` row. It is NOT a mislabel bug. It is a separate orphan class — see follow-up below.

## Root cause: the migration script

The archived migration script `scripts/archive/migrate_s3_keys.py` (committed at `fbf8e38`) ran:

```python
cur.execute(
    "SELECT id, s3_key, content_hash, format::text "
    "FROM documents WHERE s3_key IS NOT NULL"
)
rows = cur.fetchall()
...
for doc_id, old_key, content_hash, fmt in rows:
    new_key = compute_new_key(old_key, content_hash, fmt)
    # compute_new_key: prefix + content_hash + "." + ext
    ...
    s3.copy_object(
        Bucket=BUCKET,
        CopySource={"Bucket": BUCKET, "Key": old_key},
        Key=new_key,
    )
    cur.execute("UPDATE documents SET s3_key = %s WHERE id = %s", (new_key, doc_id))
```

### Why this produced mislabeled objects

At migration time, the `documents` table already contained **split-child rows** from multi-case PDF counties (Santa Clara, Orange, Riverside, Fresno). The ingestion worker's split path writes one `documents` row per extracted case inside a multi-case PDF, and sets `content_hash` to a **synthetic value** derived from the parent PDF's hash plus the split index:

```python
# packages/scraper-framework/src/ingestion/worker.py:2030
content_hash = hashlib.sha256(
    f"{content_hash}:ruling:{split_index}".encode()
).hexdigest()
```

So a multi-case PDF at `old_key = ca/santa_clara/.../raw/2026/03/12/<uuid>.pdf` with, say, 8 extracted cases would have 8 rows in `documents` — each sharing the same `old_key` (the raw PDF) but with 8 *different* synthetic `content_hash` values. The migration SELECT returned all 8 rows; for each, `new_key` was built from the synthetic hash and `CopyObject` copied the raw PDF bytes to that (wrong) key.

Result: each multi-case PDF produced N copies under N different text-derived S3 keys, **none** of which match the actual bytes hash. The S3 object metadata `content-hash` was preserved across the CopyObject (default `MetadataDirective=COPY`) and still carries the original parent PDF's correct bytes hash — which is what makes the forensic identification possible.

### Why the metadata `content-hash` field is trustworthy

`CopyObject` with default `MetadataDirective=COPY` preserves all user-defined metadata on the source object. The source object was written by the live scraper (pre-migration), which sets `content-hash: doc.content_hash` at put time with `doc.content_hash = sha256_hex(doc.raw_content)`. That value was correct at source-write time and the migration did not touch it. So the metadata field is the authoritative "what is this object" pointer.

### Why the live scraper cannot produce mislabels

`S3Archiver.archive()` in `packages/scraper-framework/src/framework/storage.py` reads `doc.content_hash` once for the key and once for the metadata within a single synchronous call — same value both times, never shared across docs, never mutated between. `BaseScraper._process_document` sets `doc.content_hash = sha256_hex(doc.raw_content)` immediately before archiving and never mutates `doc.raw_content` afterward. Verified post-migration captures confirm this empirically.

The ingestion worker's synthetic split-child content_hash never flows back into S3 write paths — it only exists in the `documents` table for FK-satisfaction on `rulings`. The migration script was the only code path that read split-child content_hash and used it to build S3 keys.

## Scope of affected objects

The migration report said "6,350 documents migrated, 0 errors" — that's the count of DB rows re-keyed. For multi-case-PDF counties, 6,350 rows ≠ 6,350 unique S3 objects. Upper bound on mislabeled objects: `(#split_child_rows) - (#distinct_parent_pdfs)`. Affected counties: **Santa Clara, Orange, Riverside, Fresno** (all the multi-case-PDF counties per `CLAUDE.md` "Split-children on rebuild").

Spot-check observations (from #2638 and this investigation):
- SC: 3 byte-identical copies under 3 different keys for one Dept 6 PDF; 1 additional identified (`e3fd8c…`).
- OC: 2 byte-identical copies under 2 different keys for one Dept C24 PDF.

A one-shot `aws s3 ls` + head-object pass per county can enumerate the full set.

## Why #2638's original hypotheses were all wrong

1. **"doc.content_hash mutated between build_s3_key and put_object"** — no shared `doc` state; `S3Archiver.archive()` reads `doc.content_hash` synchronously.
2. **"doc.raw_content mutated after hashing"** — the one mutation point (HTML CSS inlining in `_process_document`) happens *before* the hash is computed.
3. **"Two Document instances with different content_hash share the same s3_key"** — `build_s3_key` is injective on `content_hash`.
4. **"Non-framework S3 writes"** — yes, but the specific one was not a Lambda or retry — it was the 2026-03-28 migration script.
5. **"PUT retried with new body, same key"** — no such retry path exists in the scraper code.

## Resolution

- **No scraper code change needed.** The live scraper already uses correct content-addressing.
- **Cleanup required** for the historical migration artifacts. See follow-up issue.
- **Secondary orphan class** (correctly-labeled, missing DB row) is unresolved. See follow-up issue.
- **Preventative control:** add an invariant test or runtime guard that `sha256(s3_bytes) == s3_key_hash` is enforced wherever S3 objects are copied or re-keyed. See follow-up issue.
