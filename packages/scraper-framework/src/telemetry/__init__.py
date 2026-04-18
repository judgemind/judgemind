"""Telemetry event schemas and constants for the ingestion pipeline.

This package collects the canonical names of data-quality / pipeline
telemetry events emitted by ``ingestion.worker`` and related modules.
Event names are kept alongside their emitter (e.g. the
``NON_RULING_PDF_DROPPED_EVENT`` constant lives in
``ingestion.ruling_guards``) — this package exists primarily to host
the human-readable schema documentation (see ``README.md``) so dashboard
queries and alert owners have one place to consult.
"""
