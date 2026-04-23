# nlp-pipeline

NLP processing pipeline for Judgemind court documents. Handles document classification, entity extraction, summarization, embedding generation, and version diffing. This package implements the Tier 1 AI capabilities described in the architecture spec.

## Key Entry Points

- **`src/classification/classifier.py`** -- Document type and motion type classification using LLM or rule-based methods.
- **`src/entity_extraction/`** -- Named entity extraction (judges, attorneys, parties) from court document text.
- **`src/summarization/`** -- AI-generated document summaries (one-paragraph summaries cached at ingestion time).
- **`src/embedding/`** -- Vector embedding generation using open-source models (sentence-transformers). Stored in Qdrant for semantic search.
- **`src/version_diff/`** -- LLM-based classification of document revisions as substantive or cosmetic.

## What It Consumes (Inputs)

- **Document text** -- Plain text extracted from court documents (HTML, PDF, DOCX).
- **LLM APIs** -- Anthropic and OpenAI for classification, summarization, and version diffing.
- **PostgreSQL** -- Reads document and entity data via SQLAlchemy. Connection via `DATABASE_URL`.
- **Redis Streams** -- Planned: consume `document.validated` events for pipeline processing.

## What It Produces (Outputs)

- **Structured extraction results** -- Classified document types, extracted entities, motion types.
- **Document summaries** -- Cached in PostgreSQL `rulings.summary` column.
- **Vector embeddings** -- Stored in Qdrant for semantic search and RAG retrieval.
- **Version classifications** -- Substantive vs. cosmetic change labels for revised documents.

## Current State

The classification and entity extraction modules are implemented. Summarization, embedding generation, and version diffing are defined but not yet integrated into the production ingestion pipeline. Currently, field extraction is handled inline by the `scraper-framework` ingestion worker (Tier 2 LLM extraction in `ingestion/llm_extract.py`). The plan is to migrate these capabilities into this package as standalone pipeline stages consuming Redis Streams events.

## Install, Test, and Run Locally

This package depends on the local sibling `judgemind-config`, which is not published to PyPI. Use the helper script — it creates the venv and installs `judgemind-config` first, then this package with `[dev]` extras:

```bash
# From the repo root:
scripts/install-package-venv.sh nlp-pipeline

# Lint and format (run from packages/nlp-pipeline)
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/

# Run tests
.venv/bin/pytest tests/ -v
```

See `docs/specs/architecture-spec-v1.md` Section 5 for the full AI/ML layer architecture.
