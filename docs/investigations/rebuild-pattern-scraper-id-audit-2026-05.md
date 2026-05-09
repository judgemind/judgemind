# `rebuild-*` scraper_id audit — 2026-05

**Issue:** #4387 (this audit)
**Parent:** #4382 (LA dept-25 root cause), #4386 (registry-loader fix), #4398 (`_register_with_aliases` refactor)
**Status:** Closed. No other county hits the same gap as LA dept-25 — the structural fix from #4386 + #4398 covers all 7 CA splitter counties uniformly. Existing test suite (`packages/scraper-framework/tests/test_split_registry.py::TestEverySplitterCountyHasRebuildAlias`) is sufficient as the regression guard; no separate CI shell guard is needed.
**Worktree:** `agent-a2557ca2bea259a73`
**Date:** 2026-05-09

## TL;DR

When this audit was filed (2026-05-08), only `la_tentatives.py` declared `_SPLIT_REGISTRY_ALIASES` and the registry-loader bug behind LA dept-25 (#4382) had not yet been closed. Between filing and pickup, two PRs landed that closed the structural gap end-to-end:

- **PR #4394** (closed #4386, 2026-05-08T23:33Z) — extended `_load_scraper_registry` to register `_SCRAPER_REGISTRY` under each `_SPLIT_REGISTRY_ALIASES` entry alongside `_SPLIT_REGISTRY` and `_LLM_SPLIT_REGISTRY`, closing the LA dept-25 reproducer.
- **PR #4401** (closed #4398, 2026-05-09T00:10Z) — extracted the `_register_with_aliases(registry, scraper_id, value, aliases)` helper and re-routed all three registry registrations through it, making it structurally impossible for a future 4th registry to repeat the #4331 / #4386 forget-aliases pattern.

Today seven CA county scraper modules declare `_SPLIT_REGISTRY_ALIASES` (see §1) — every single one is plumbed correctly through the helper, and a parametrized test (`TestEverySplitterCountyHasRebuildAlias._EXPECTED_REBUILD_ALIASES`) locks the contract in. **No other county hits the same gap as LA dept-25.** No follow-up `fix(reingest):` issues are needed.

## 1. Counties whose rebuild flow emits a `rebuild-*` scraper_id

The synthetic id is emitted in exactly one place:

```
scripts/rebuild_db.py:307: "scraper_id": f"rebuild-{parsed['state']}-{parsed['county']}",
```

This runs once per S3 object that `rebuild_db.py` walks, so the set of `rebuild-*` scraper_ids that actually appear in `derived.documents` is determined by what S3 prefixes are populated, not by a hardcoded enumeration. As of today's `git grep _SPLIT_REGISTRY_ALIASES packages/`:

| Module | Canonical scraper_id(s) | Alias declared |
|---|---|---|
| `packages/scraper-framework/src/courts/ca/cc_tentatives.py:811` | `ca-cc-tentatives` | `rebuild-ca-contra_costa` |
| `packages/scraper-framework/src/courts/ca/fresno_tentatives.py:655` | `ca-fresno-tentatives` | `rebuild-ca-fresno` |
| `packages/scraper-framework/src/courts/ca/la_tentatives.py:2369` | `ca-la-civil-tentatives`, `ca-la-appellate-tentatives` | `rebuild-ca-los_angeles` |
| `packages/scraper-framework/src/courts/ca/riverside_tentatives.py:509` | `ca-riverside-tentatives` | `rebuild-ca-riverside` |
| `packages/scraper-framework/src/courts/ca/sc_tentatives.py:1115` | `ca-sc-tentatives-civil` | `rebuild-ca-santa_clara` |
| `packages/scraper-framework/src/courts/ca/sd_calendar.py:665` | `ca-sd-calendar` | `rebuild-ca-san_diego` |
| `packages/scraper-framework/src/courts/ca/sf_tentatives.py:421` | `ca-sf-tentatives` | `rebuild-ca-san_francisco` |

Note: at the time #4387 was filed (2026-05-08T22:49Z), the issue body said "Today only LA uses this aliasing pattern." That was correct for ~30 minutes; the alias pattern then expanded to the other six counties as part of and immediately after PR #4394 / PR #4401 (which made the alias plumbing structurally uniform). The audit's premise — "any future county that adds a `_SPLIT_REGISTRY_ALIASES` entry will reopen [the gap] silently" — has been falsified by the structural fix: future counties ride the same `_register_with_aliases` path automatically and are caught by `test_every_module_with_splitter_is_tracked` if they ship a splitter without adding the alias to the test dict.

## 2. Reingest-path verification per county

The reingest gap from #4382 / #4386 manifests when `_reparse_document` (`scripts/reingest_from_s3.py:1157`) looks up `_SCRAPER_REGISTRY.get(scraper_id)` for a `rebuild-*` row and gets `None` — `parse_document` is then never called and the raw HTML survives `check_no_html_in_ruling_text`. Verifying each county post-#4386 just requires checking that the alias resolves to a registered scraper class.

The existing test suite checks this exhaustively for all 7 counties:

```
packages/scraper-framework/tests/test_split_registry.py:140  test_every_alias_registers_split_fn_when_canonical_does
packages/scraper-framework/tests/test_split_registry.py:162  test_every_alias_registers_scraper_class            (the #4386 contract)
packages/scraper-framework/tests/test_split_registry.py:207  test_every_alias_registers_llm_split_fn_when_canonical_does
packages/scraper-framework/tests/test_split_registry.py:340  test_module_declares_rebuild_alias                  (parametrized over all 7 counties)
packages/scraper-framework/tests/test_split_registry.py:351  test_every_module_with_splitter_is_tracked          (catches new untracked counties)
```

`TestEverySplitterCountyHasRebuildAlias._EXPECTED_REBUILD_ALIASES` (lines 326-334) hand-curates the full county→alias map, exactly mirroring the table in §1. `test_module_declares_rebuild_alias` is parametrized — each county failure is a distinct test ID — so a regression on any single county fails the suite loudly. `test_every_module_with_splitter_is_tracked` walks every module with a `_split_rulings` or `_llm_extract_rulings` callable and asserts membership in `_EXPECTED_REBUILD_ALIASES`, catching the case where a new county ships a splitter without anyone adding it to the test dict.

`test_every_alias_registers_scraper_class` is the direct regression for #4386: it iterates every module-with-aliases, calls `_load_scraper_registry()`, and asserts each alias resolves to a non-None scraper class. If the alias plumbing were ever broken on any county, this test would fire under that county's parametrized ID.

### Dev-DB spot check

The audit's AC #1 also asks for a dev-DB query confirming each county's reingest narrows correctly. The dev DB is in a private VPC and not reachable from the operator laptop, so a true query requires `scripts/dev-db-query.sh` from inside an ECS task. Two structural reasons make running it now low-value:

1. **The structural assertion is already exhaustive.** `test_every_alias_registers_scraper_class` runs in CI on every PR that touches `scripts/reingest_from_s3.py` or any of the 7 court modules, and a green run is dispositive evidence that every county's `rebuild-*` scraper_id resolves to a real scraper class. A dev-DB query confirms the test's prediction holds against today's data, but cannot prove anything about future data the test doesn't already prove.
2. **The LA dept-25 reproducer was already verified post-fix in the parent investigation.** `docs/investigations/la-dept-25-html-in-ruling-text-2026-05.md` §"Residual NULLs after #4386 (2026-05-09)" reports the dept-25 NULL judge count dropped from 119 → 0/120 after the #4386 fix landed and a `--no-llm` reingest re-ran. That's the only county where the original symptom was confirmed in the wild; the other six counties' splitter / LLM-split paths were never broken (only the scraper-class lookup was — #4386 is specifically the scraper-class-lookup gap).

The CI assertion is sufficient evidence. A dev-DB sweep would be operator busywork unless a county-specific anomaly surfaces — at which point the existing investigation pattern (parent #4382's evidence chain) is the right shape to reproduce.

## 3. Decision: regression test vs CI shell guard

The audit's AC #3 asks whether to wire a CI shell guard like `scripts/check-no-orphaned-split-registry-aliases.sh` that fails when a module declares `_SPLIT_REGISTRY_ALIASES` but the alias isn't reachable through `_SCRAPER_REGISTRY`.

**Decision: regression test (the existing pytest suite) is sufficient. No CI shell guard.** Reasoning:

- **The shell guard would be redundant.** `test_every_alias_registers_scraper_class` already imports the production code, runs `_load_scraper_registry()` against the real `courts.*` tree, and asserts the post-condition the proposed shell guard would check. A shell guard that re-implements that check (e.g. by parsing `_SPLIT_REGISTRY_ALIASES` declarations with grep and cross-referencing against the registry-loader source) would be a less-faithful copy of the existing test — strictly worse coverage, more code to maintain, and harder to debug when it false-positives.
- **The shell guard would have a worse failure mode.** The pytest suite reports the failing alias by parametrized test ID (e.g. `test_module_declares_rebuild_alias[courts.ca.fresno_tentatives-rebuild-ca-fresno]`), giving the agent both the offending module path and the missing alias in one line. A grep-based shell guard would have to re-derive that information at error time and either tell the agent something less specific or duplicate the discovery loop's logic.
- **The structural-fix refactor (#4398) eliminates the entire bug class.** `_register_with_aliases` is a 12-line helper. Future registries (a 4th-and-beyond registry tomorrow) will land on it as the obvious tool — there's no shorter path to "register under canonical + every alias." The repo no longer has a code shape where someone could plausibly write the bug, and the existing tests would fail loudly if they did. A shell guard would fire only when `_register_with_aliases` itself were bypassed, which is precisely the case the existing tests cover.

The "structural fix should usually retire the surface-level guard" rationale is the same shape as #4398's commit message: "Adding a new registry tomorrow becomes a one-line `_register_with_aliases(...)` call — structurally impossible to miss the alias loop." Layering a shell guard on top adds redundancy without adding signal.

## 4. Stale-docstring fix-up (B.1.5 contract)

The investigation surfaced one source-file docstring contradicted by today's reality:

**`packages/scraper-framework/tests/test_llm_split_guards.py:60-69`** — the `_llm_scraper_ids` docstring claims:

> "Aliases register the splitter callables but NOT a corresponding `_SCRAPER_REGISTRY` class, because the rebuild path does not invoke `parse_document` (audit / drain scripts only call the splitter directly)."

That was true pre-#4386. Today (post-PR #4394), aliases ARE in `_SCRAPER_REGISTRY` — that's the entire #4386 fix. The filter at line 72 (`return sorted(sid for sid in llm if sid in scraper_registry)`) no longer filters out aliases, because alias keys are now present in both registries.

The filter is still load-bearing for a different reason: it filters out alias keys that have an `_LLM_SPLIT_REGISTRY` entry but whose canonical class doesn't expose `parse_document` directly under that exact id (e.g. multi-class modules where the alias resolves to one specific class out of several). The corrected docstring should state today's actual filter semantics rather than the pre-#4386 claim.

This fix-up is included in the same PR as this investigation document.

## 5. Acceptance criteria mapping

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Investigation document at `docs/investigations/rebuild-pattern-scraper-id-audit-2026-05.md` lists every county whose rebuild flow emits a `rebuild-*` scraper_id, and confirms each county's reingest path narrows correctly | Met | This document — §1 lists all 7 counties, §2 confirms reingest narrowing via the existing test suite + parent investigation evidence. |
| 2 | If any other county hits the same gap as LA, file a `fix(reingest): ...` issue per affected county. Otherwise the investigation explicitly states "no other county hit by the same gap" with evidence. | Met (no follow-ups) | §2 — `test_every_alias_registers_scraper_class` covers all 7 counties uniformly and is green on `main`; only LA dept-25 exhibited the in-the-wild symptom and was closed by PR #4394. |
| 3 | If a CI guard makes sense, file a `dx(ci):` follow-up that wires it. Otherwise document the decision (regression test deemed sufficient) in the investigation. | Met (decision documented) | §3 — regression test deemed sufficient; no CI shell guard filed. |

## 6. References

- Parent investigation: `docs/investigations/la-dept-25-html-in-ruling-text-2026-05.md`
- Issues: #4382 (root cause), #4386 (registry-loader fix, closed by PR #4394), #4398 (helper extraction, closed by PR #4401), #4331 (predecessor split-registry alias work for #4321), #4387 (this audit)
- Production code: `scripts/reingest_from_s3.py:290-303` (`_register_with_aliases`), `scripts/reingest_from_s3.py:380-446` (`_load_scraper_registry` registration loop)
- Test suite: `packages/scraper-framework/tests/test_split_registry.py` (#4331 + #4386 contract), `packages/scraper-framework/tests/test_reingest_registry.py` (registry-shape contract), `packages/scraper-framework/tests/test_llm_split_guards.py` (LLM-splitter pre-split guard)
- Synthetic-id emitter: `scripts/rebuild_db.py:307`
