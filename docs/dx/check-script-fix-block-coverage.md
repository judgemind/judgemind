# Hygiene-Guard Fix-Block Coverage

Status of every `scripts/check-*.{sh,py}` hygiene guard with respect to the
"emit a copy-pasteable Fix block alongside the violation list" pattern that
landed in PR #4345 / issue #4322 and was generalised by issue #4346.

The motivation: when CI fails on a hygiene-guard, agents (and humans) burn
iterations re-discovering the canonical fix. A guard that emits the
`Fix:`-block pattern from PR #4345 short-circuits the re-discovery — the
first read of the failing CI job names the fix, copy-pasteable.

## Verdicts

- **self-diagnosing (Fix block)** — guard already emits an explicit
  `Fix:` / `Fix options:` / `Remediation:` block with concrete instructions
  or copy-pasteable patch literal. No upgrade needed.
- **self-diagnosing (actionable text)** — guard emits actionable text in
  the violation report (e.g. "rename one or delete the stale sibling",
  "Add `continue-on-error: true`") but no labelled `Fix:` block. Adequate
  today; could be upgraded to canonical `Fix:` shape if friction warrants.
- **wrapper (delegates to helper)** — sh script that `exec python3
  scripts/<helper>.py "$@"` — fix-block lives in the helper or is emitted
  by the helper's stderr. Verdict mirrors the helper.
- **operational health probe** — guard checks live state (API key,
  task-def fingerprint, image build), not source-code hygiene. The fix is
  judgment-driven (rotate the key, redeploy, re-register the task-def);
  no mechanical patch literal applies.
- **decision flow (no violation list)** — guard emits a decision (duplicate
  PR found / not / shipped) for callers, not a violation list. Fix-block
  pattern doesn't apply.
- **NEEDS UPGRADE** — guard emits only "X is wrong at file:line" with no
  actionable Fix text, AND the canonical fix has a deterministic shape
  worth automating.

## Survey

| # | Guard | Verdict | Notes |
|---|-------|---------|-------|
| 1 | `scripts/check-api-keys.sh` | operational health probe | Validates external API keys (Anthropic/Google) — fix is rotate the key in Secrets Manager. |
| 2 | `scripts/check-apollo-keyfields.sh` | self-diagnosing (Fix block) | Emits "Fix: Add ... to typePolicies" with two literal forms. |
| 3 | `scripts/check-aws-bool-flags.sh` | self-diagnosing (Fix block) | Emits "Example fix:" with BAD/GOOD pairs. |
| 4 | `scripts/check-aws-flag-portability.sh` | self-diagnosing (Fix block) | Emits replacement-pattern examples. |
| 5 | `scripts/check-bare-shadcn-accent.sh` | wrapper (delegates to helper) | Helper `_check_bare_shadcn_strip_pairs.py` emits `ERROR_HEADER` with token-pair fix mappings. |
| 6 | `scripts/check-bash-compat.sh` | self-diagnosing (Fix block) | **Upgraded #4346:** emits per-construct rewrite suggestions inline with each violation. Prior shape only pointed at docs/agent/code-standards.md. |
| 7 | `scripts/check-bash-set-u-empty-array.sh` | self-diagnosing (Fix block) | Emits replacement pattern. |
| 8 | `scripts/check-blocked-issues.sh` | self-diagnosing (actionable text) | "Fix by adding 'Blocked by #N' to the issue body, or use scripts/block-issue.sh ...". |
| 9 | `scripts/check-brand-md-tokens.sh` | self-diagnosing (Fix block) | Emits canonical token list. |
| 10 | `scripts/check-case-type-fallback-parity.sh` | wrapper (delegates to helper) | Wrapper for `check-case-type-fallback-parity.py`. |
| 11 | `scripts/check-ci-job-skipped.sh` | wrapper (delegates to helper) | Wrapper for `check-ci-job-skipped.py`. |
| 11a | `scripts/check-ci-guards-skip-list-coverage.sh` | self-diagnosing (Fix block) | Meta-check guarding `run-ci-guards.sh`'s SKIP_LIST: fails when a guard is missing from SKIP_LIST AND has no `# ci-guards: skip` marker. Detects four required-kinds — `argparse required=True` / `add_mutually_exclusive_group(required=True)`, top-level `os.environ["VAR"]` reads, shell `${1:?}` strict positional, and shell `${VAR:?}` strict env var (file-scope only). Emits per-violation `Fix:` block tagged with the detected required-kind so operators see whether the trigger was a CLI argument or an environment variable. Tracking: #4384 (env-var extension), #4379 (parent meta-check, parent: #4332). |
| 12 | `scripts/check-ci-passed-coverage.sh` | wrapper (delegates to helper) | Wrapper for `check-ci-passed-coverage.py`. |
| 13 | `scripts/check-cleanup-step-continue-on-error.sh` | self-diagnosing (Fix block) | Inline Python emits "Add `continue-on-error: true` at the step level". |
| 14 | `scripts/check-cloudwatch-alarm-docs.sh` | self-diagnosing (actionable text) | "Add a row to the §'CloudWatch Alarms' table with prefix, module, source metric, ...". Could be upgraded to emit a literal markdown-row patch. |
| 14a | `scripts/check-deploy-workflow-rollout.sh` | self-diagnosing (Fix block) | Validates that every `.github/workflows/deploy-*.yml` workflow that pushes an image to ECR also rolls the running ECS service / scheduler — flags the silent-drift class behind #2772 (build-and-push job shipped without a rollout sibling, every subsequent merge silently drifts the live image from the running task). Detects `docker push` / `aws ecr put-image` as the push signal and `aws ecs update-service` / `aws scheduler update-schedule` / `aws ecs register-task-definition` / `uses: ./.github/actions/ecs-deploy` as the rollout signal. Workflows that are intentionally build-only (e.g. `deploy-dispatcher-v3.yml` — image lands now, task-def re-registers and service rollouts land in follow-up issues) opt out via the literal `# deploy-rollout-lint: build-only` comment paired with a justification. Emits a `Fix:` block naming the four canonical rollout signals plus the opt-out marker recipe. Wired by `deploy-workflow-rollout-check` in `.github/workflows/ci.yml` (gated on `detect-changes.outputs.deploy-workflows == 'true'`) and the `.githooks/pre-push` hook on `.github/workflows/deploy-*.yml` / `scripts/check-deploy-workflow-rollout.sh` changes. Tracking: #2777 (this guard), #2772 (root-cause incident). |
| 15 | `scripts/check-deprecated-models.sh` | self-diagnosing (Fix block) | Emits literal model-name replacement table. |
| 16 | `scripts/check-diff-coverage.sh` | self-diagnosing (actionable text) | "Add tests for those lines, then re-run: scripts/check-diff-coverage.sh ..." with replay command. |
| 17 | `scripts/check-dispatcher-execution-mode-aware.sh` | self-diagnosing (Fix block) | Emits required-pattern examples. |
| 18 | `scripts/check-dispatcher-image-deps.sh` | wrapper (delegates to helper) | Wrapper for `check-dispatcher-image-deps.py`. |
| 19 | `scripts/check-dispatcher-image-versions.sh` | operational health probe | Builds the dispatcher image and runs `python --version`/`node --version`. Fix is bump the base image. |
| 20 | `scripts/check-dispatcher-terminal-statuses.sh` | self-diagnosing (Fix block) | Emits literal SQL constraint to add. |
| 21 | `scripts/check-dispatcher-terminals-consistent.sh` | self-diagnosing (Fix block) | Emits canonical terminal list. |
| 21a | `scripts/check-dispatcher-test-imports.sh` | self-diagnosing (Fix block) | Forbids the absolute-package import shape (`from scripts.dispatcher import ...` / `import scripts.dispatcher`) inside `scripts/dispatcher/tests/*.py`. The shape works under pytest's local default rootdir but fails in CI as `ModuleNotFoundError: No module named 'scripts'` (#4417 / PR #4425). Emits a `Fix:` block with the canonical sibling-import replacement: `_SCRIPTS = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(_SCRIPTS)); from dispatcher import X`. Scope is confined to `scripts/dispatcher/tests/` — `packages/*/tests/` use a different import resolution and are out of scope. Tracking: #4429. |
| 22 | `scripts/check-duplicate-functions.sh` | wrapper (delegates to helper) | Wrapper for `check-duplicate-functions.py`. |
| 23 | `scripts/check-duplicate-pr.sh` | decision flow (no violation list) | Returns 0/1/2 with a single status line for callers (`/task` skill). No code violation. |
| 23a | `scripts/check-fix-block-coverage-complete.sh` | self-diagnosing (Fix block) | Inventory-coverage guard: fails when a runnable hygiene guard exists in the tree without a matching row here. Emits per-violation `Fix:` block naming the doc, the verdict vocabulary, and the letter-suffix-row pattern. Mirrors `run-ci-guards.sh --list` discovery so .py companions are covered by their .sh wrapper's row. Tracking: #4367. |
| 24 | `scripts/check-git-gh-retries.sh` | self-diagnosing (Fix block) | Emits the `git_with_retry` / `gh_with_retry` wrapper pattern. |
| 25 | `scripts/check-graphql-nullability-drift.sh` | wrapper (delegates to helper) | Wrapper for `check-graphql-nullability-drift.py`. |
| 26 | `scripts/check-graphql-queries.sh` | self-diagnosing (Fix block) | Emits the `mock` / `__typename` add suggestion. |
| 27 | `scripts/check-hardcoded-colors.sh` | self-diagnosing (Fix block) | Emits `text-foreground` / token replacements. |
| 28 | `scripts/check-hardcoded-models.sh` | self-diagnosing (Fix block) | Emits canonical-name replacement. |
| 29 | `scripts/check-hyphen-underscore-collision.sh` | self-diagnosing (Fix block) | **Upgraded #4346:** emits a `Fix:` block with concrete `git mv` rename suggestion picking a canonical winner per file extension (`.py`/`.tf` ⇒ underscore; `.sh`/`.md`/dirs ⇒ hyphen). Prior shape said only "Rename one, or delete the stale sibling". |
| 30 | `scripts/check-ingestion-worker-task-def-fingerprint.sh` | self-diagnosing (Fix block) | Emits "Recovery:" with copy-paste `aws ecs describe-task-definition ...` + `update-service --force-new-deployment` recipe. |
| 31 | `scripts/check-issue-author.sh` | decision flow (no violation list) | Returns trust verdict for callers. The fix (move untrusted issue to triage) is enacted by callers, not by this guard. |
| 31a | `scripts/check-issue-verify-sql.py` | self-diagnosing (Fix block) | Validates SQL columns referenced in `Verify:` lines of an issue body against `packages/api/src/data-access/schema.sql`. Emits per-violation `column <schema.table.col> does not exist` plus a `Fix:` block pointing at `schema.sql` and `scripts/dev-db-query.sh`. Tracking: #4358. |
| 32 | `scripts/check-llm-json-loads.sh` | self-diagnosing (Fix block) | Emits the `_safe_json_loads` / try-except replacement. |
| 33 | `scripts/check-llm-paths-symmetry.sh` | self-diagnosing (actionable text) | Emits "the chunk-failure event MUST include document_id= so the log line is self-diagnosing". Could be upgraded to a literal-patch suggestion. |
| 34 | `scripts/check-markdown-links.sh` | wrapper (delegates to helper) | Wrapper for `check-markdown-links.py`. |
| 35 | `scripts/check-migration-files.sh` | self-diagnosing (Fix block) | **Upgraded #4346:** emits a `Fix:` block with concrete `git mv N_*.sql <expected>_*.sql` rename suggestions for naming-pattern, duplicate-number, and gap errors. Prior shape only printed the violation. |
| 36 | `scripts/check-migration-number-collision.sh` | wrapper (delegates to helper) | Wrapper for `check-migration-number-collision.py` whose `format_collision()` already emits a 5-step Remediation block. |
| 37 | `scripts/check-no-api-github-fetch.sh` | self-diagnosing (Fix block) | Emits the `gh` CLI replacement. |
| 37a | `scripts/check-no-basicconfig-with-extra.sh` | self-diagnosing (Fix block) | AST-walks every top-level `scripts/*.py` and flags files that call `logging.basicConfig(...)` AND pass `extra=` to a logger method AND do NOT also call `configure_structlog(...)`. Catches the #4368 bug class — `basicConfig(format="%(asctime)s %(levelname)-8s %(message)s")` silently drops every `extra=` field from CloudWatch output. Emits a `Fix:` block naming the canonical `from framework.logging import configure_structlog` + `configure_structlog(json=True, stdlib_bridge=True)` replacement and citing `scripts/drain_splitter_carry_forward_clusters.py` (PR #4368) as the reference implementation. Wired by `no-basicconfig-with-extra-check` in `.github/workflows/ci.yml` (gated on `detect-changes.outputs.scripts == 'true'`). Tracking: #4376 (this guard), #4368 (root-cause incident), #4373 (bulk migration of pre-existing affected scripts). |
| 38 | `scripts/check-no-duplicate-stubs.sh` | self-diagnosing (Fix block) | Emits "remove the duplicate" guidance with file:line pairs. |
| 39 | `scripts/check-no-ecs-wait-services-stable.sh` | self-diagnosing (Fix block) | Emits the `wait-for-deploy.sh` replacement. |
| 40 | `scripts/check-no-git-repo-root-anchor.sh` | self-diagnosing (Fix block) | Emits the `git -C "$REPO_ROOT"` replacement. |
| 41 | `scripts/check-no-gnu-head.sh` | self-diagnosing (Fix block) | Emits `head -n -N` → `sed '$d'` / awk equivalents. |
| 42 | `scripts/check-no-graphql-instanceof.sh` | self-diagnosing (Fix block) | Emits `__typename` check replacement. |
| 43 | `scripts/check-no-heredoc-pipe-shadow.sh` | self-diagnosing (Fix block) | Emits "Fix recipes" with three concrete patterns. |
| 44 | `scripts/check-no-inline-ecs-healthcheck.sh` | self-diagnosing (Fix block) | Emits the `terraform-managed healthcheck` block. |
| 44a | `scripts/check-no-logging-basicconfig.sh` | self-diagnosing (Fix block) | Validates that top-level `scripts/*.py` files use `from framework.logging import configure_structlog` + `configure_structlog(json=True, stdlib_bridge=True)` instead of `logging.basicConfig(`. Emits a `Fix:` block with the canonical one-line replacement diff plus references to the post-#4368 / post-#4373 reference implementations (`scripts/drain_splitter_carry_forward_clusters.py` and `scripts/audit_correctly_labeled_s3_orphans.py`). Files needing genuine basicConfig opt out via the `# basic-config-allow:` marker — today's only allowlist entry is `scripts/telemetry_upload.py`. Scope is top-level only (no `-r`); nested subdirs (`spotcheck/`, `dispatcher/`, `dispatcher_v3/`, `archive/`) live under separate observability conventions. Strictly stronger than #37a (`check-no-basicconfig-with-extra.sh`, which only flags basicConfig when paired with `extra=`) — this guard catches the latent-regression case where a script uses basicConfig but does not yet call `extra=`. Tracking: #4400 (this guard), #4368 (root cause), #4373 / #4399 (precursor migrations), #4376 (narrower companion guard). |
| 44b | `scripts/check-no-ci-classifier-duplication.sh` | self-diagnosing (Fix block) | Forbids spelling out the CI rollup-classifier conclusion vocabulary (`frozenset({"FAILURE", "TIMED_OUT", ...})` / `failure_conclusions = {...}` / jq `$x == "FAILURE" or $x == "TIMED_OUT"` / awk `"FAILURE" \|\| "TIMED_OUT"`) outside the canonical `scripts/dispatcher/phase_transitions.py` + `scripts/dispatcher/ci_classifier_cli.py`. Pre-#4417 the rule was duplicated across four sites and the same bug class — CANCELLED handling — bit twice (#4407 → PR #4411 for `wait-for-ci.sh`, #4414 → PR #4415 for the four daemon-side sites). Emits a `Fix:` block naming the canonical Python helper / CLI to import or pipe into, plus the allowlist-extension escape hatch for legitimate new tests. `wait-for-ci.sh` is allowlisted (intentionally Bash-only for /task agents); a fixture-parity test in `test_ci_classifier_cli.py` keeps it from drifting. Tracking: #4417 (this guard + the broader refactor). |
| 45 | `scripts/check-no-nonascii-tf-descriptions.sh` | self-diagnosing (Fix block) | Emits ASCII-replacement table. |
| 46 | `scripts/check-no-opensearch-url-fallback.sh` | self-diagnosing (Fix block) | Emits the centralised-config replacement. |
| 47 | `scripts/check-no-rebuild-db-skip-reset.sh` | self-diagnosing (Fix block) | Emits `--skip-reset` removal guidance. |
| 48 | `scripts/check-no-redos-pattern.sh` | self-diagnosing (Fix block) | Emits "Fix options" with three patterns + `# noqa: redos-pattern` suppression. |
| 49 | `scripts/check-no-test-database-url-fallback.sh` | self-diagnosing (Fix block) | Emits required-shape example. |
| 50 | `scripts/check-no-test-leaked-worktrees.sh` | self-diagnosing (Fix block) | Emits cleanup-pattern recipe. |
| 50a | `scripts/check-no-tmp-oneshot-file-path-derivation.py` | self-diagnosing (Fix block) | Validates that `scripts/*.py` files which derive a data-file path from `Path(__file__).resolve().parent.parent` either (a) carry an existence-probe guard (`is_file()` / `is_dir()` / `exists()`) in the same scope as the unsafe data-load call, (b) have a module-scope `assert` / `if not ...is_dir(): raise` against the derived path, (c) wrap the unsafe call in `try: ...; except OSError`, or (d) carry the `# oneshot-path-required: <reason>` opt-out marker. Catches the #4374 bug class — under `scripts/ecs-run-task.sh` the script lands at `/tmp/_oneshot_script` and `parent.parent` collapses to `/`, silently producing a wrong path. Emits per-violation `Fix options:` block naming the candidate-path fallback pattern from `drain_splitter_carry_forward_clusters.py` and the at-import assert form. No `.sh` wrapper — invoked directly via `python3 scripts/check-no-tmp-oneshot-file-path-derivation.py` from CI and pre-push. Tracking: #4381 (this guard), #4374 (root-cause incident). |
| 50b | `scripts/check-no-unbounded-timeouts.py` | self-diagnosing (Fix block) | Validates IO call sites under `scripts/dispatcher/` carry bounded timeouts (boto3, psycopg, requests, urllib3). Emits per-violation `Fix: add the missing kwarg, or annotate the call with ...` block. No `.sh` wrapper — invoked directly via `python3 scripts/check-no-unbounded-timeouts.py` from CI and pre-push. Tracking: #3353 (root cause), #4364/#4365 (pre-push wiring). |
| 51 | `scripts/check-nullable-column-reads.sh` | wrapper (delegates to helper) | Wrapper for `check-nullable-column-reads.py`. |
| 52 | `scripts/check-oneshot-imports.sh` | self-diagnosing (Fix block) | Emits "Fix: inline the helper" guidance. |
| 53 | `scripts/check-oneshot-repo-paths.sh` | self-diagnosing (Fix block) | Emits the `Path(__file__).resolve().parent.parent` replacement. |
| 54 | `scripts/check-parse-document-reingest-safety.sh` | self-diagnosing (Fix block) | Emits required-marker substring list + canonical-example file paths. |
| 55 | `scripts/check-per-phase-timeouts.sh` | wrapper (delegates to helper) | Wrapper for `check-per-phase-timeouts.py`. |
| 56 | `scripts/check-placeholder-gates.sh` | self-diagnosing (Fix block) | Emits "remove the placeholder, ship the real check". |
| 57 | `scripts/check-pr-title.sh` | self-diagnosing (actionable text) | Emits "Remediation: task-v2-summary did not run — re-trigger or amend manually". |
| 58 | `scripts/check-rebase-no-silent-drop.sh` | self-diagnosing (Fix block) | Emits `git rebase --abort` + investigation steps. |
| 59 | `scripts/check-removed-exports.sh` | self-diagnosing (Fix block) | Emits "Either restore the export or update the importers" with a file list. |
| 60 | `scripts/check-repo-walk-exclusions-canonical.sh` | self-diagnosing (Fix block) | Emits "Required shape:" with literal `source preflight.sh` + iteration block. |
| 61 | `scripts/check-scraper-image-shipped.sh` | self-diagnosing (Fix block) | Emits "Did you push the image to ECR?" recipe. |
| 62 | `scripts/check-scraper-registry.sh` | wrapper (delegates to helper) | Wrapper for `check-scraper-registry.py`. |
| 62a | `scripts/check-scraper-zero-record-runner.py` | operational health probe | Scheduled-cron ECS runner for the zero-record streak detector — wraps `check-scraper-zero-record-streak.py` and manages GitHub-issue lifecycle (open/comment/close) on breach. Fix is to investigate the scraper that triggered the alert — no source-code patch literal applies. Wired by `.github/workflows/scraper-zero-record-check.yml`. Tracking: #2620 / #2666. |
| 62b | `scripts/check-scraper-zero-record-streak.py` | operational health probe | Scheduled-cron data-quality probe — queries `telemetry.scraper_runs` for consecutive zero-record streaks per scraper. Fires when any active scraper has been silently outputting no data for N runs. Fix is to investigate the scraper, not patch the check. Wired by `.github/workflows/scraper-zero-record-check.yml`. Tracking: #2620 / #2666. |
| 63 | `scripts/check-script-headers.sh` | wrapper (delegates to helper) | Wrapper for `check-script-headers.py` which emits the `# one-off: true` / `# permanent: true` add-this guidance. |
| 64 | `scripts/check-shard-coverage.sh` | self-diagnosing (Fix block) | Emits the `--shard-of-N` invocation hint. |
| 65 | `scripts/check-shipped-pr.sh` | decision flow (no violation list) | Returns shipped/not-shipped verdict for callers. No code violation. |
| 65a | `scripts/check-short-unsubstantive-rulings.py` | operational health probe | Scheduled-cron data-quality probe — counts per-county rulings with `char_length(ruling_text) < 200 AND motion_type IS NULL AND outcome IS NULL` over the last 7 days. Fires when any county exceeds the configured threshold (signal that extraction is silently dropping useful fields). Fix is to investigate the extraction pipeline for that county; no source-code patch literal applies. Wired by `.github/workflows/short-unsubstantive-ruling-check.yml`. Tracking: #2671. |
| 66 | `scripts/check-split-ruling-fields-propagated.sh` | wrapper (delegates to helper) | Wrapper for `check_split_ruling_fields_propagated.py` whose `_suggest_scope_entry()` emits the canonical Fix block (the reference upgrade from PR #4345). |
| 66a | `scripts/check-sql-columns.py` | self-diagnosing (Fix block) | Validates qualified (`alias.column`) and unqualified column references in Python SQL string literals against `packages/api/src/data-access/schema.sql`. Emits per-violation `Fix: check the table's column definitions in schema.sql.` block. No `.sh` wrapper — invoked directly via `python3 scripts/check-sql-columns.py` from CI (`sql-column-check` job). Tracking: #1929 (qualified-drift), #4271 (unqualified-drift). |
| 67 | `scripts/check-sql-conflicts.sh` | wrapper (delegates to helper) | Wrapper for `check-sql-conflicts.py`. |
| 68 | `scripts/check-subprocess-timeouts.sh` | self-diagnosing (Fix block) | Emits `timeout=N` argument suggestion. |
| 69 | `scripts/check-task-recovery.sh` | decision flow (no violation list) | Emits per-phase resume hints — that IS the fix-guidance, not a violation list. |
| 70 | `scripts/check-terminal-routing-comments.sh` | self-diagnosing (Fix block) | Emits required-comment-shape pattern. |
| 71 | `scripts/check-terraform-ecs-entrypoint.sh` | self-diagnosing (Fix block) | **Upgraded #4346:** delegates to `check_tf_ecs_entrypoint.py` which now emits a per-violation patch literal naming the actual interpreter and resource. Wrapper Fix block remains as the generic backstop. |
| 72 | `scripts/check-terraform-empty-resource-risk.sh` | self-diagnosing (Fix block) | **Upgraded #4346:** delegates to `check_tf_empty_resource.py` which now emits a per-violation patch literal naming the actual variables in the `compact([...])` list. Wrapper Fix block remains as the generic backstop. |
| 73 | `scripts/check-test-except-pass.sh` | wrapper (delegates to helper) | Wrapper for `check-test-except-pass.py`. |
| 74 | `scripts/check-test-statuscode-assertions.sh` | wrapper (delegates to helper) | Wrapper for `check-test-statuscode-assertions.py`. |
| 75 | `scripts/check-tests-use-reingest-helper.sh` | self-diagnosing (Fix block) | Emits `make_reingest_cap_doc(...)` template. |
| 76 | `scripts/check-transition-dispatch-vocabulary.sh` | self-diagnosing (Fix block) | Emits the canonical-vocabulary list. |
| 77 | `scripts/check-vitest-environment-deps.sh` | self-diagnosing (Fix block) | Emits the `npm install` invocation. |
| 78 | `scripts/check-workflow-paths-filter-coverage.sh` | wrapper (delegates to helper) | Wrapper for `check-workflow-paths-filter-coverage.py`. |
| 78b | `scripts/check_fix_block_coverage_complete.py` | self-diagnosing (Fix block) | Per-guard Fix-block formatter for `check-fix-block-coverage-complete.sh` (#23a). Parses the inventory in this file, computes the alphabetical insertion point + letter-suffix row number for each missing guard, and emits a copy-pasteable row template plus the new `Total guards: N` count. Invoked from the wrapper when the diff produces a non-empty missing list. Tracking: #4405 (this guard), #4376 (parent retro). |
| 78a | `scripts/check_no_basicconfig_with_extra.py` | self-diagnosing (Fix block) | Emits `<path>:<lineno>:logging.basicConfig + extra= at line(s) ...` per violating file; wrapper sh adds the `Fix:` block naming the canonical `configure_structlog(json=True, stdlib_bridge=True)` replacement. Tracking: #4376. |
| 79 | `scripts/check_no_redos_pattern.py` | self-diagnosing (Fix block) | Emits `<path>:<lineno>:<pattern>` per violation; wrapper sh adds the Fix-options block. |
| 80 | `scripts/check_parse_document_reingest_safety.py` | self-diagnosing (Fix block) | Emits `<path>:<lineno>:<label>` per violation; wrapper sh adds the required-marker block. |
| 81 | `scripts/check_split_ruling_fields_propagated.py` | self-diagnosing (Fix block) | Reference upgrade from PR #4345 — emits per-violation Fix block with the `_DATACLASS_SCOPE` patch literal. |
| 82 | `scripts/check_tests_use_reingest_helper.py` | self-diagnosing (Fix block) | Emits `<path>:<lineno>:CapturedDocument(...)` per violation; wrapper sh adds the helper-template block. |
| 83 | `scripts/check_tf_ecs_entrypoint.py` | self-diagnosing (Fix block) | **Upgraded #4346:** emits per-violation `Fix:` block with literal `entryPoint = ["<interpreter>"]` patch + the actual `command =` carryover, naming the resource and container. |
| 84 | `scripts/check_tf_empty_resource.py` | self-diagnosing (Fix block) | **Upgraded #4346:** emits per-violation `Fix:` block with literal `count = length(local.compacted_<name>) > 0 ? 1 : 0` patch + a `locals { compacted_<name> = compact([...]) }` template, naming the actual variables. |

## Summary

- Total guards: 99 (#14a `check-deploy-workflow-rollout.sh` added by #2777; #31a `check-issue-verify-sql.py` added by #4358; #23a `check-fix-block-coverage-complete.sh`, #50b `check-no-unbounded-timeouts.py`, #62a `check-scraper-zero-record-runner.py`, #62b `check-scraper-zero-record-streak.py`, #65a `check-short-unsubstantive-rulings.py`, #66a `check-sql-columns.py` added by #4367; #11a `check-ci-guards-skip-list-coverage.sh` added by #4379; #50a `check-no-tmp-oneshot-file-path-derivation.py` added by #4381; #37a `check-no-basicconfig-with-extra.sh` + #78a `check_no_basicconfig_with_extra.py` added by #4376; #78b `check_fix_block_coverage_complete.py` added by #4405; #44a `check-no-logging-basicconfig.sh` added by #4400; #21a `check-dispatcher-test-imports.sh` added by #4429).
- Already self-diagnosing (Fix block or actionable text) before #4346: 71.
- Wrappers (delegate to helper): 18.
- Operational health probes: 5.
- Decision flows: 4.
- **Upgraded by #4346: 5** (#6 `check-bash-compat.sh`, #29 `check-hyphen-underscore-collision.sh`, #35 `check-migration-files.sh`, #71 `check_tf_ecs_entrypoint.py`, #72 `check_tf_empty_resource.py`).
- No future upgrade currently warranted: the remaining "actionable text" guards (#8, #14, #16, #33, #57) have human-readable guidance that's already concrete enough; upgrading them to literal-patch shape would add noise relative to the friction they cause.

## How this list is maintained

This survey was authored manually for the #4346 audit (2026-05-08). Future
contributors:

- When you add a new `scripts/check-*.{sh,py}` guard: append a row. To
  minimise renumbering churn use a letter-suffix row number at the
  alphabetical insertion point (e.g. `23a`, `50a`, `66a`) — the
  `scripts/check-fix-block-coverage-complete.sh` CI guard (#23a) fails
  the build if you forget.
- When you upgrade a guard's error output: update the Verdict and Notes.
- When you delete a guard: remove the row.

The audit's `/audit` skill (§1.9) counts top-level scripts via marker
headers; this doc is a per-guard health check the audit can cross-reference
against the count. The `scripts/check-fix-block-coverage-complete.sh` guard
(wired into CI as `fix-block-coverage-complete-check`) catches drift at PR
time — see #4367.
