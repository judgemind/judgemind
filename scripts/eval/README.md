# LLM Extraction Eval Scripts

Scripts for evaluating LLM field extraction accuracy on California court tentative rulings. These compare model quality and cost across different providers and prompt versions, using the same test fixtures that the scraper regression tests use.

Built during investigation [#418](https://github.com/judgemind/judgemind/issues/418). Results feed into cost/quality decisions for the ingestion pipeline.

## Eval Suite (recommended)

The structured eval suite (`run_extraction_eval.py` + `eval_scorer.py`) is the primary way to evaluate extraction quality. It runs `LlmExtractor` against test fixtures, scores results per-field and per-county, and enforces quality thresholds.

### Quick start

```bash
# Live mode: run LlmExtractor against fixtures (requires ANTHROPIC_API_KEY)
python3 scripts/eval/run_extraction_eval.py --live --model haiku

# Cached mode: score previously-cached results (CI-friendly, no API calls)
python3 scripts/eval/run_extraction_eval.py --cached --model haiku

# Compare two models side-by-side
python3 scripts/eval/run_extraction_eval.py --compare haiku sonnet

# Check quality thresholds (exits non-zero on failure)
python3 scripts/eval/run_extraction_eval.py --cached --model haiku --check-thresholds

# JSON output for programmatic consumption
python3 scripts/eval/run_extraction_eval.py --cached --model haiku --output-format json
```

### Quality thresholds

| Metric | Threshold |
|--------|-----------|
| `case_number` accuracy | >= 95% |
| `case_title` accuracy | >= 90% |
| `outcome` accuracy | >= 90% |
| `ruling_text` similarity | >= 95% |
| `party_recall` | >= 85% |
| Hallucinations | 0 |

### Architecture

- **`eval_scorer.py`** -- Pure scoring functions: field normalization, comparison, text similarity, hallucination detection, per-county aggregation, threshold checking, and reporting. Fully tested (70 unit tests in `packages/scraper-framework/tests/test_eval_scorer.py`).
- **`run_extraction_eval.py`** -- CLI runner with two modes: `--live` (runs `LlmExtractor`, saves cached JSON results) and `--cached` (loads cached results, scores without API calls). Supports model comparison and threshold enforcement.

### Output files

Results are saved to `scripts/eval/results/` (gitignored):
- `{model}_cached.json` -- Raw extraction results from live runs.
- `{model}_scores.json` -- Scored results with per-fixture and per-county breakdowns.

## Legacy scripts

| Script | Model(s) | Prompt | Purpose |
|--------|----------|--------|---------|
| `haiku_eval_v1.py` | Haiku, Sonnet, Opus | v1 (confidence scores) | Original multi-model comparison + determinism test |
| `haiku_eval_v2.py` | Haiku | v2 (multi-ruling, metadata) | Production prompt evaluation |
| `gemini_flash_eval.py` | Gemini 2.5 Flash, Flash Lite | v2 | Cross-provider cost comparison |

## Setup

Install the required dependencies (these are **not** part of the project's standard deps):

```bash
# For the eval suite and Anthropic evals
pip install anthropic pymupdf

# For Gemini evals (legacy scripts only)
pip install google-genai pymupdf
```

Set the appropriate API key:

```bash
# Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# Google (for Gemini legacy scripts)
export GOOGLE_API_KEY="AIza..."
```

## Output

Each script prints detailed results to stdout (per-field accuracy, mismatches, cost projections) and saves raw JSON results to `scripts/eval/results/`. The results directory is gitignored since it contains run-specific data.

## Test Fixtures

All scripts use the same fixtures from `packages/scraper-framework/tests/fixtures/` and their expected outputs in `fixtures/expected/`. Index pages, error pages, and access-denied fixtures are automatically skipped.

## Known Fixture Issues

Some mismatches are caused by inconsistencies in the test fixtures themselves, not model errors. These are tracked in `KNOWN_FIXTURE_ISSUES` within each script and excluded from the "adjusted accuracy" metric.
