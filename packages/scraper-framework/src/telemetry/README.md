# Ingestion Pipeline Telemetry Events

This document lists the structured telemetry events emitted by the
ingestion pipeline so dashboards, alerts, and on-call runbooks can
rely on stable names and field shapes.

Each entry describes:

- **Event name** — value of the ``telemetry_event`` field on the
  structured log line.
- **Emitter** — source file / function that emits the event.
- **Metric name** — corresponding ``metric_name`` in
  ``telemetry.data_quality_metrics`` (short form, no dotted namespace).
- **When it fires** — conditions that produce the event.
- **Fields** — shape of the structured log ``extra`` and/or the
  ``metadata`` JSON column in ``telemetry.data_quality_metrics``.

## `data_quality.non_ruling_pdf_dropped`

**Emitter:** `packages/scraper-framework/src/ingestion/worker.py` — pipeline-side
all-NULL metadata guard in `process_event`, immediately after
deterministic validation and before the DB upsert.

**Metric name:** `non_ruling_pdf_dropped` (written to
`telemetry.data_quality_metrics`).

**When it fires:** all six core metadata fields — `case_number`,
`case_title`, `judge_name`, `department`, `motion_type`, `outcome` —
are NULL / empty / whitespace-only for a ruling that survived LLM
extraction.  A synthetic placeholder `case_number` matching the
`UNKNOWN-*` prefix is treated as NULL for this check.

This is a defense-in-depth guard.  Capture-side filters introduced in
[#2486] skip non-ruling PDFs (admin notices, cover sheets, "no tentative
rulings today" pages) before they reach the ingestion worker.  This
event is the pipeline-side fallback: when a non-ruling PDF slips past
the scraper (new county adds an admin-notice URL, an upstream filter
regresses, a one-off capture backfill runs without filters), the worker
drops the row and emits this event instead of inserting metadata-free
noise into `derived.rulings`.

**Structured log fields** (`extra` on the warning log entry):

| Field           | Type           | Description                                      |
|-----------------|----------------|--------------------------------------------------|
| `document_id`   | `str`          | Worker-assigned document ID.                     |
| `county`        | `str`          | Normalized county slug (e.g. `fresno`).          |
| `state`         | `str`          | Two-letter state code (e.g. `ca`).               |
| `scraper_id`    | `str`          | Source scraper identifier.                       |
| `s3_key`        | `str \| None`  | S3 key of the raw capture that produced the row. |
| `telemetry_event` | `str`        | Always `data_quality.non_ruling_pdf_dropped`.    |

**`telemetry.data_quality_metrics` row:**

```
metric_name = 'non_ruling_pdf_dropped'
metric_value = 1
metadata = {
  "document_id": "<uuid>",
  "s3_key": "<s3 key or null>",
  "state": "ca",
  "scraper_id": "<scraper id>"
}
```

**Related events:**

- `data_quality.ruling_empty_text_dropped` — deterministic validation
  earlier in the pipeline drops rulings whose `ruling_text` is empty
  regardless of metadata state (see [#2646]).
- `zero_ruling_extraction` — emitted when the LLM returns zero rulings
  for a document (see [#1337], [#2569]).  The `non_ruling_pdf_dropped`
  guard runs *after* a ruling has been extracted, so the two paths do
  not overlap.

**Rationale:** see [#2676] for the defense-in-depth motivation and
[#2486] for the capture-side filter set this event complements.

[#2486]: https://github.com/judgemind/judgemind/issues/2486
[#2569]: https://github.com/judgemind/judgemind/issues/2569
[#2646]: https://github.com/judgemind/judgemind/issues/2646
[#2676]: https://github.com/judgemind/judgemind/issues/2676
[#1337]: https://github.com/judgemind/judgemind/issues/1337
