# LLM Extraction Eval Scripts

Scripts for evaluating LLM field extraction accuracy on California court tentative rulings. These compare model quality and cost across different providers and prompt versions, using the same test fixtures that the scraper regression tests use.

Built during investigation [#418](https://github.com/judgemind/judgemind/issues/418). Results feed into cost/quality decisions for the ingestion pipeline.

## Scripts

| Script | Model(s) | Prompt | Purpose |
|--------|----------|--------|---------|
| `haiku_eval_v1.py` | Haiku, Sonnet, Opus | v1 (confidence scores) | Original multi-model comparison + determinism test |
| `haiku_eval_v2.py` | Haiku | v2 (multi-ruling, metadata) | Production prompt evaluation |
| `gemini_flash_eval.py` | Gemini 2.5 Flash, Flash Lite | v2 | Cross-provider cost comparison |

## Setup

Install the required dependencies (these are **not** part of the project's standard deps):

```bash
# For Anthropic (Haiku/Sonnet/Opus) evals
pip install anthropic pymupdf

# For Gemini evals
pip install google-genai pymupdf
```

Set the appropriate API key:

```bash
# Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# Google (for Gemini)
export GOOGLE_API_KEY="AIza..."
```

## Running

Run from anywhere -- paths are resolved automatically relative to the repo root:

```bash
# Haiku v2 eval (recommended — uses the production prompt)
python3 scripts/eval/haiku_eval_v2.py

# Gemini Flash comparison
python3 scripts/eval/gemini_flash_eval.py

# Original v1 multi-model eval (runs Haiku + Sonnet + Opus — expensive)
python3 scripts/eval/haiku_eval_v1.py
```

## Output

Each script prints detailed results to stdout (per-field accuracy, mismatches, cost projections) and saves raw JSON results to `scripts/eval/results/`. The results directory is gitignored since it contains run-specific data.

## Test Fixtures

All scripts use the same fixtures from `packages/scraper-framework/tests/fixtures/` and their expected outputs in `fixtures/expected/`. Index pages, error pages, and access-denied fixtures are automatically skipped.

## Known Fixture Issues

Some mismatches are caused by inconsistencies in the test fixtures themselves, not model errors. These are tracked in `KNOWN_FIXTURE_ISSUES` within each script and excluded from the "adjusted accuracy" metric.
