#!/usr/bin/env python3
# venv: none (stdlib only)
# one-off: true
"""Build phase-input fixtures for dispatcher v2 spike 0.3.

Reads real data from issue #2513 and PR #2534, writes a realistic
input payload per phase into fixtures/<phase>.json. Also produces
byte/char/estimated-token counts per fixture in fixtures/sizes.json.

Token estimation: 1 token ~ chars/3.5 for mixed English+code text.
This is a rough heuristic; real counts come from running claude -p
--output-format stream-json against the fixtures and reading the
usage.input_tokens field per turn.

Throwaway alongside the rest of the dispatcher-spike infra (see #2699).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Fixtures live under the current worktree's tmp/ (the agent running this
# script sets {worktree}/tmp/fixtures as their working location). Using the
# repo-relative path ensures this works from any checkout — not just the
# agent who ran spike 0.3 first.
REPO_ROOT = Path(__file__).resolve().parents[2]
FIX_DIR = REPO_ROOT / "tmp" / "spike-0.3-fixtures"
FIX_DIR.mkdir(parents=True, exist_ok=True)

# Need the real PR #2534 diff on disk before running the fixture build.
# The test suite and CI path for the spike harness captures `gh pr diff 2534`
# once and commits it under scripts/dispatcher-spike/pr2534.diff so we have
# a reproducible source of truth.

# -- real data captured from the live issue + PR ----------------------

ISSUE_2513_TITLE = "fix(scraping): apply post-processing filters on LLM cache-hit paths"
ISSUE_2513_BODY = """## Problem

The LLM cache in `packages/scraper-framework/src/framework/llm_extractor.py` stores the **post-filter** rulings under the PDF/text content hash. When a cache hit occurs, the cached rulings are returned directly **without re-running the post-processing filters** (`_drop_calendar_listing_rulings`, `_filter_citation_artifacts`, `_deduplicate_ruling_texts`, `_resolve_cross_references`). Paths affected:

- `LLMExtractor.extract_from_pdf` - cache read at lines 1296-1300
- `LLMExtractor.extract_from_text` - cache read at lines 1192-1196

Observed impact: #2489 widened `_drop_calendar_listing_rulings` to catch 7 new listing-only patterns. After merging #2489 (commit dfe10a8) and running `rebuild_db.py --county Orange --reset`, the OC short-ruling count (<100 chars) only dropped from ~397 to 161 - far above the `<50` target. The 161 remaining rows are exactly the patterns the widened filter now classifies as listings. Running `reingest_from_s3.py --county Orange --bust-llm-cache` works but costs 22-45 hours of LLM calls for the full OC corpus.

## Fix

Apply the post-processing filters on the cache-hit paths, not just on fresh extraction. In both `extract_from_pdf` and `extract_from_text`:

```python
if self._cache is not None and not effective_bust:
    cached = self._cache.get(PDF_PER_PAGE_PROMPT, content_key)
    if cached is not None:
        logger.debug("llm_cache.hit_pdf", content_key=content_key[:12])
        rulings = [ExtractedRuling(**r) for r in cached]
        # Apply post-processing filters (idempotent - safe to re-run).
        rulings = _drop_calendar_listing_rulings(rulings)
        rulings = _deduplicate_ruling_texts(rulings)
        return rulings
```

The filters are idempotent: applying them twice (at cache write in `_join_page_rows`, and at cache read) has no effect if the rulings already passed the filter. But if the filter was updated since the cache entry was written, the new logic takes effect on cache read - avoiding expensive reingest.

Note: `_resolve_cross_references` and `_propagate_document_fields` in `_join_page_rows` cannot run at cache read time because they need the pre-join row state. Those are fine to keep only on the fresh path. The filters that operate purely on the final `ExtractedRuling[]` list are the ones to re-apply.

## Acceptance criteria

- [ ] `extract_from_pdf` re-applies `_drop_calendar_listing_rulings` and `_deduplicate_ruling_texts` on cache-hit path.
- [ ] `extract_from_text` re-applies `_filter_citation_artifacts` and `_deduplicate_ruling_texts` on cache-hit path.
- [ ] Regression test: cache a set of rulings that include a pattern NOT in the filter, then update the filter to catch it, clear and re-read cache, verify the updated filter applies.
- [ ] After merge + deploy + `rebuild_db.py --county Orange --reset`, OC short-ruling count drops to < 50. Verify: `scripts/dev-db-query.sh "SELECT COUNT(*) FROM rulings r JOIN documents d ON r.document_id = d.id JOIN courts c ON c.id = d.court_id WHERE c.county = 'Orange' AND LENGTH(r.ruling_text) < 100"` returns `< 50`.
- [ ] Diff coverage on changed lines >= 90%.

## Related

- #2489 - widened the filter; blocked on this for dev-DB verification
- #2446 - original OC calendar-listing filter issue
- #2424 - `--bust-llm-cache` flag (the workaround for this bug)

Parent: #2489
"""

ISSUE_2513_COMMENTS = [
    {
        "author": "drewthaler",
        "date": "2026-04-17T06:40:00Z",
        "body": "Picking this up in agent-a8dcbb09.",
    },
]

# Real diff from PR #2534 — fetched once and committed to the scripts dir.
DIFF_PATH = Path(__file__).resolve().parent / "pr2534.diff"
if not DIFF_PATH.exists():
    raise SystemExit(
        f"Missing {DIFF_PATH}. Run `gh pr diff 2534 --repo judgemind/judgemind "
        f"> scripts/dispatcher-spike/pr2534.diff` before invoking this script."
    )
GIT_DIFF = DIFF_PATH.read_text(encoding="utf-8")

RALPH_SUMMARY = (
    "Added two idempotent helpers (`_apply_pdf_cache_hit_filters`, "
    "`_apply_text_cache_hit_filters`) and invoked them on the cache-hit branches "
    "of `extract` (text path) and `extract_from_pdf` (PDF path). PDF helper "
    "applies `_drop_calendar_listing_rulings`, `_deduplicate_ruling_texts`, and "
    "`_filter_citation_artifacts`. Text helper applies `_filter_citation_artifacts` "
    "and `_deduplicate_ruling_texts`. Both emit `cache_hit_filters_dropped` info "
    "logs when they drop rows."
)

ACCEPTANCE_CRITERIA = [
    "`extract_from_pdf` re-applies `_drop_calendar_listing_rulings` and `_deduplicate_ruling_texts` on cache-hit path.",
    "`extract_from_text` re-applies `_filter_citation_artifacts` and `_deduplicate_ruling_texts` on cache-hit path.",
    "Regression test: cache a set of rulings that include a pattern NOT in the filter, then update the filter to catch it, clear and re-read cache, verify the updated filter applies.",
    "After merge + deploy + `rebuild_db.py --county Orange --reset`, OC short-ruling count drops to < 50.",
    "Diff coverage on changed lines >= 90%.",
]

# -- simulated CI failure log for the fix-ci fixture ----------------------

CI_FAIL_LOG_TAIL = (
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.12.3, pytest-8.0.0, pluggy-1.4.0\n"
    "rootdir: /workspace/packages/scraper-framework\n"
    "collected 47 items\n\n"
    "tests/test_llm_cache_hit_filters.py::TestApplyPdfCacheHitFilters::test_drops_off_calendar_listing PASSED [ 2%]\n"
    "tests/test_llm_cache_hit_filters.py::TestApplyPdfCacheHitFilters::test_drops_bare_disposition_parenthetical PASSED [ 4%]\n"
    "tests/test_llm_cache_hit_filters.py::TestApplyPdfCacheHitFilters::test_deduplicates_identical_ruling_texts PASSED [ 6%]\n"
    "tests/test_llm_cache_hit_filters.py::TestApplyPdfCacheHitFilters::test_preserves_distinct_rulings PASSED [ 8%]\n"
    "tests/test_llm_cache_hit_filters.py::TestExtractFromPdfCacheHitReappliesFilters::test_cache_hit_drops_calendar_listing_rulings FAILED [10%]\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "___________ TestExtractFromPdfCacheHitReappliesFilters.test_cache_hit_drops_calendar_listing_rulings ___________\n"
    "\n"
    "    def test_cache_hit_drops_calendar_listing_rulings(self):\n"
    "        extractor = LlmExtractor(api_key='test-key', cache=stub_cache)\n"
    "        result = extractor.extract_from_pdf(pdf_bytes=b'stub', content_key='abc123')\n"
    ">       assert len(result) == 1, f'expected 1 ruling after filter, got {len(result)}'\n"
    "E       AssertionError: expected 1 ruling after filter, got 8\n"
    "E       assert 8 == 1\n"
    "E        +  where 8 = len([<ruling>, <ruling>, ...])\n"
    "\n"
    "tests/test_llm_cache_hit_filters.py:142: AssertionError\n"
    "----------------------------- Captured log call -----------------------------\n"
    "INFO     scraper_framework.llm_extractor:llm_extractor.py:1398 llm_cache.hit_pdf content_key=abc123\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_llm_cache_hit_filters.py::TestExtractFromPdfCacheHitReappliesFilters::test_cache_hit_drops_calendar_listing_rulings - AssertionError\n"
    "========================= 46 passed, 1 failed in 3.42s =========================\n"
)

# -- phase fixtures --------------------------------------------------------


def write(name: str, payload: dict) -> None:
    path = FIX_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# /task-v2-plan input
write(
    "plan_input",
    {
        "issue_number": 2513,
        "issue_title": ISSUE_2513_TITLE,
        "issue_body": ISSUE_2513_BODY,
        "issue_comments": ISSUE_2513_COMMENTS,
        "worktree_path": "/workspace/worktrees/agent-xxxx",
        "repo_root": "/workspace",
    },
)

# /task-v2-ralph input
write(
    "ralph_input",
    {
        "plan": {
            "go": True,
            "plan_text": (
                "Add two idempotent helpers `_apply_pdf_cache_hit_filters` and "
                "`_apply_text_cache_hit_filters` in "
                "`packages/scraper-framework/src/framework/llm_extractor.py`. "
                "Invoke them on the cache-hit branches of `extract` (text path) "
                "and `extract_from_pdf` (PDF path). PDF helper runs "
                "`_drop_calendar_listing_rulings` -> `_deduplicate_ruling_texts` "
                "-> `_filter_citation_artifacts`. Text helper runs "
                "`_filter_citation_artifacts` -> `_deduplicate_ruling_texts`. "
                "Both emit `llm_extractor.cache_hit_filters_dropped` info logs when "
                "they actually drop rows. Add 17 unit + integration tests in a new "
                "file `tests/test_llm_cache_hit_filters.py` covering the two helpers "
                "individually, both cache-hit call sites, and a regression scenario "
                "that simulates filter widening (cache populated under an older/"
                "narrower filter; current filter correctly drops stale rows on read)."
            ),
            "acceptance_criteria": ACCEPTANCE_CRITERIA,
            "scope_check": [
                {
                    "search_pattern": "self._cache.get",
                    "locations_found": [
                        "packages/scraper-framework/src/framework/llm_extractor.py:1192 (extract text path)",
                        "packages/scraper-framework/src/framework/llm_extractor.py:1296 (extract_from_pdf)",
                    ],
                    "in_scope": True,
                },
            ],
            "relevant_files": [
                "packages/scraper-framework/src/framework/llm_extractor.py",
                "packages/scraper-framework/tests/test_llm_cache_hit_filters.py",
            ],
            "relevant_docs": [
                "docs/specs/architecture-spec-v1.md#section-5.2",
            ],
        },
        "issue_number": 2513,
        "worktree_path": "/workspace/worktrees/agent-xxxx",
        "repo_root": "/workspace",
        "max_iterations": 5,
    },
)

# /task-v2-summary input
write(
    "summary_input",
    {
        "issue_number": 2513,
        "issue_title": ISSUE_2513_TITLE,
        "issue_body": ISSUE_2513_BODY,
        "issue_comments": ISSUE_2513_COMMENTS,
        "ralph_summary": RALPH_SUMMARY,
        "changed_files": [
            "packages/scraper-framework/src/framework/llm_extractor.py",
            "packages/scraper-framework/tests/test_llm_cache_hit_filters.py",
        ],
        "git_diff": GIT_DIFF,
        "worktree_path": "/workspace/worktrees/agent-xxxx",
    },
)

# /task-v2-fix-ci input
write(
    "fix_ci_input",
    {
        "pr_number": 2534,
        "branch": "agent-a8dcbb09",
        "failing_jobs": [
            {
                "name": "test-scraper-framework",
                "conclusion": "failure",
                "log_tail": CI_FAIL_LOG_TAIL,
            },
        ],
        "git_diff_base_to_head": GIT_DIFF,
        "worktree_path": "/workspace/worktrees/agent-xxxx",
    },
)

# /task-v2-verify input
write(
    "verify_input",
    {
        "issue_number": 2513,
        "pr_number": 2534,
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "change_type": "scraper",
        "touched_services": [
            "/ecs/judgemind-scraper-dev",
            "/ecs/judgemind-ingestion-worker-dev",
            "derived.rulings",
        ],
        "worktree_path": "/workspace/worktrees/agent-xxxx",
        "repo_root": "/workspace",
    },
)

# /task-v2-retro input — based on the real run
write(
    "retro_input",
    {
        "issue_number": 2513,
        "pr_number": 2534,
        "phase_transitions": [
            {"phase": "claiming", "duration_s": 18, "outcome": "ok"},
            {"phase": "setup", "duration_s": 92, "outcome": "ok"},
            {"phase": "ralph-worker (1)", "duration_s": 1823, "outcome": "ok"},
            {"phase": "ralph-reviewer (1)", "duration_s": 412, "outcome": "REVISE"},
            {"phase": "ralph-worker (2)", "duration_s": 687, "outcome": "ok"},
            {"phase": "ralph-reviewer (2)", "duration_s": 298, "outcome": "SHIP"},
            {"phase": "pushing", "duration_s": 27, "outcome": "ok"},
            {"phase": "ci-watch (1)", "duration_s": 1320, "outcome": "SUCCESS"},
            {"phase": "merging", "duration_s": 11, "outcome": "ok"},
            {"phase": "deploying", "duration_s": 714, "outcome": "ok"},
            {"phase": "verifying", "duration_s": 2095, "outcome": "ok"},
        ],
        "failures": [],
        "ralph_iterations": 2,
        "ci_attempts": 1,
        "total_duration_s": 6497,
        "diff_stats": {
            "files_changed": 2,
            "insertions": 594,
            "deletions": 2,
        },
    },
)

# -- size report ----------------------------------------------------------

sizes = {}
for fname in sorted(os.listdir(FIX_DIR)):
    if not fname.endswith(".json") or fname == "sizes.json":
        continue
    path = FIX_DIR / fname
    text = path.read_text(encoding="utf-8")
    # Rough token heuristic: Anthropic's English/code tokenizer ~ chars/3.5
    est_tokens = round(len(text) / 3.5)
    sizes[fname.removesuffix(".json")] = {
        "bytes": path.stat().st_size,
        "chars": len(text),
        "est_input_tokens": est_tokens,
    }

# Also size the skill files themselves — claude -p loads them as context.
skill_dir = REPO_ROOT / ".claude" / "skills"
for skill in [
    "task-v2-plan",
    "task-v2-ralph",
    "task-v2-summary",
    "task-v2-fix-ci",
    "task-v2-verify",
    "task-v2-retro",
]:
    path = skill_dir / skill / "SKILL.md"
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    sizes[f"skill:{skill}"] = {
        "bytes": path.stat().st_size,
        "chars": len(text),
        "est_input_tokens": round(len(text) / 3.5),
    }

(FIX_DIR / "sizes.json").write_text(json.dumps(sizes, indent=2), encoding="utf-8")
print(json.dumps(sizes, indent=2))
