# Pre-push vs. CI guard parity audit (2026-05)

## TL;DR

**Every `scripts/check-*.{sh,py}` guard invoked in the CI `scripts-tests (python)` job today already runs in `.githooks/pre-push` via the auto-discovering umbrella `scripts/run-ci-guards.sh`** (closed by issue #4332, lines 908-939 of the pre-push hook). 100% parity. No follow-up "wire to pre-push" issues are needed because the structural fix has already landed and is self-maintaining for new guards.

The audit's premise — `scripts/check-subprocess-timeouts.sh` was wired one-off in #4328, and we should walk the rest of the CI guards individually — was overtaken by #4332, which was filed at 17:28 UTC on 2026-05-08 (this issue, #4333, was filed at 17:30 UTC) and merged at 18:08 UTC. #4332 generalised the per-check pattern into a single umbrella that auto-discovers any new `scripts/check-*.{sh,py}` and runs every applicable guard from pre-push, with a `SKIP_CI_GUARDS=1` env-var bypass for the rare partial-work case. That makes per-check audits redundant by construction.

## Method

1. Extract every `scripts/check-*.{sh,py}` invocation in the `scripts-tests (python)` matrix shard of `.github/workflows/ci.yml` (lines 627-985, `if: matrix.shard == 'python'`):
   ```
   grep -nE 'run:' .github/workflows/ci.yml | awk -F: '$1 >= 700 && $1 <= 985' \
     | grep -oE 'check-[a-z0-9-]+\.(sh|py)' | sort -u
   ```
   Result: 26 unique check-script invocations.

2. List every guard the pre-push umbrella discovers and runs:
   ```
   scripts/run-ci-guards.sh --list
   ```
   Result: 86 runnable guards (30 skipped — see "Built-in skips" below).

3. Diff:
   ```
   comm -23 ci_scripts_tests_python.txt umbrella_runs.txt
   ```
   Result: empty. Every CI scripts-tests guard is covered by the umbrella.

## Per-guard parity index (CI scripts-tests (python) shard — 26 guards)

This list is the canonical bullet-style index that `grep -E '^- '` matches against (per the issue's AC#1 verify command). Every guard listed here is covered by the pre-push umbrella `scripts/run-ci-guards.sh` (wired at `.githooks/pre-push:922-936`); the rationale table below has the per-guard details. All 26 are sub-second AST/grep checks with no external dependencies — exactly the class the umbrella was designed for.

- `check-aws-bool-flags.sh` — Wired (umbrella).
- `check-aws-flag-portability.sh` — Wired (umbrella).
- `check-bash-compat.sh` — Wired (umbrella).
- `check-bash-set-u-empty-array.sh` — Wired (umbrella).
- `check-cloudwatch-alarm-docs.sh` — Wired (umbrella).
- `check-dispatcher-execution-mode-aware.sh` — Wired (umbrella + standalone gate at `.githooks/pre-push:686-706`).
- `check-dispatcher-image-deps.sh` — Wired (umbrella + standalone gate at `.githooks/pre-push:649-663`).
- `check-dispatcher-test-imports.sh` — Wired (umbrella).
- `check-git-gh-retries.sh` — Wired (umbrella).
- `check-no-ci-classifier-duplication.sh` — Wired (umbrella).
- `check-no-duplicate-stubs.sh` — Wired (umbrella).
- `check-no-ecs-wait-services-stable.sh` — Wired (umbrella).
- `check-no-git-repo-root-anchor.sh` — Wired (umbrella + standalone gate at `.githooks/pre-push:668-680`).
- `check-no-gnu-head.sh` — Wired (umbrella).
- `check-no-heredoc-pipe-shadow.sh` — Wired (umbrella).
- `check-no-inline-ecs-healthcheck.sh` — Wired (umbrella).
- `check-no-logging-basicconfig.sh` — Wired (umbrella).
- `check-no-rebuild-db-skip-reset.sh` — Wired (umbrella).
- `check-no-test-leaked-worktrees.sh` — Wired (umbrella).
- `check-no-tmp-oneshot-file-path-derivation.py` — Wired (umbrella + standalone gate at `.githooks/pre-push:760-778`).
- `check-no-unbounded-timeouts.py` — Wired (umbrella + standalone gate at `.githooks/pre-push:724-742`).
- `check-per-phase-timeouts.sh` — Wired (umbrella).
- `check-rebase-no-silent-drop.sh` — Wired (umbrella + standalone gate at `.githooks/pre-push:893-906`).
- `check-scraper-image-shipped.sh` — Wired (umbrella).
- `check-subprocess-timeouts.sh` — Wired (umbrella + standalone gate at `.githooks/pre-push:870-884`).
- `check-terminal-routing-comments.sh` — Wired (umbrella).

## Per-guard rationale table

Same list as above with a one-line rationale per guard. The table format is the working artefact; the bullet list above is the AC#1 verify index.

| # | Guard | Pre-push status | Rationale |
|---|-------|-----------------|-----------|
| 1 | `check-aws-bool-flags.sh` | Wired (umbrella) | Self-contained shell grep. |
| 2 | `check-aws-flag-portability.sh` | Wired (umbrella) | Self-contained shell grep. |
| 3 | `check-bash-compat.sh` | Wired (umbrella) | Self-contained shell scan; macOS bash 3.2 subset enforcement. |
| 4 | `check-bash-set-u-empty-array.sh` | Wired (umbrella) | Self-contained shell scan. Sibling of #3 for set -u footguns. |
| 5 | `check-cloudwatch-alarm-docs.sh` | Wired (umbrella) | Walks `infra/terraform/modules/` + `docs/agent/infrastructure-reference.md`. Sub-second. |
| 6 | `check-dispatcher-execution-mode-aware.sh` | Wired (umbrella) + standalone gate (lines 686-706) | Also called directly from the daemon-changed gate (defense-in-depth; the standalone gate predates the umbrella). |
| 7 | `check-dispatcher-image-deps.sh` | Wired (umbrella) + standalone gate (lines 649-663) | Same. Standalone gate is the original #2776 wire-in. |
| 8 | `check-dispatcher-test-imports.sh` | Wired (umbrella) | Self-contained AST scan over `scripts/dispatcher/tests/`. |
| 9 | `check-git-gh-retries.sh` | Wired (umbrella) | AST scan over `scripts/dispatcher/daemon.py`. |
| 10 | `check-no-ci-classifier-duplication.sh` | Wired (umbrella) | Self-contained grep. Issue #4417. |
| 11 | `check-no-duplicate-stubs.sh` | Wired (umbrella) | Self-contained scan over scripts/tests/. |
| 12 | `check-no-ecs-wait-services-stable.sh` | Wired (umbrella) | Self-contained shell grep. |
| 13 | `check-no-git-repo-root-anchor.sh` | Wired (umbrella) + standalone gate (lines 668-680) | Standalone gate is original #2829 wire-in; umbrella is defense-in-depth. |
| 14 | `check-no-gnu-head.sh` | Wired (umbrella) | Self-contained grep. |
| 15 | `check-no-heredoc-pipe-shadow.sh` | Wired (umbrella) | Self-contained AST scan. Issue #4267. |
| 16 | `check-no-inline-ecs-healthcheck.sh` | Wired (umbrella) | Self-contained workflow-yaml scan. |
| 17 | `check-no-logging-basicconfig.sh` | Wired (umbrella) | Self-contained AST scan over `scripts/*.py`. Issue #4400. |
| 18 | `check-no-rebuild-db-skip-reset.sh` | Wired (umbrella) | Self-contained docs scan. |
| 19 | `check-no-test-leaked-worktrees.sh` | Wired (umbrella) | Self-contained directory scan. Issue #4307. |
| 20 | `check-no-tmp-oneshot-file-path-derivation.py` | Wired (umbrella) | Self-contained AST scan. Issue #4381. |
| 21 | `check-no-unbounded-timeouts.py` | Wired (umbrella) + standalone gate (lines 724-742) | Standalone gate is original #3356/#4364 wire-in; umbrella is defense-in-depth. |
| 22 | `check-per-phase-timeouts.sh` | Wired (umbrella) | Self-contained AST scan. Issue #3776. |
| 23 | `check-rebase-no-silent-drop.sh` | Wired (umbrella) + standalone gate (lines 893-906) | Standalone gate is the original #3670 wire-in; umbrella is defense-in-depth. |
| 24 | `check-scraper-image-shipped.sh` | Wired (umbrella) | Self-contained Dockerfile scan. Issue #4294. |
| 25 | `check-subprocess-timeouts.sh` | Wired (umbrella) + standalone gate (lines 870-884) | Standalone gate is the #4328 wire-in (the immediate motivator for this audit); umbrella is defense-in-depth. |
| 26 | `check-terminal-routing-comments.sh` | Wired (umbrella) | Self-contained AST scan. Issue #3072. |

### Standalone-gate redundancy

Six of the 26 guards (#6, #7, #13, #21, #23, #25) are wired BOTH via dedicated path-conditional gates AND via the umbrella. This is intentional. (1) The standalone gates predate the umbrella and were wired one-off as the underlying check-script was authored. They are path-filtered (e.g. only fire when `scripts/dispatcher/daemon.py` is in the diff) so they print a more targeted "checking <X>..." probe line and only spend hash time when the relevant files changed. (2) The umbrella runs unconditionally on every push and re-runs the same scripts. The duplication costs sub-second wall-clock per guard but provides fail-closed coverage if the standalone path filter ever drifts away from the script's actual scope. **Removing the standalone gates is a possible follow-up but not a defect** — the umbrella is the structural fix, the standalone gates are best-effort early-fail observability.

### Built-in skips (out of scope for the audit, listed for completeness)

The umbrella's skip list (`scripts/run-ci-guards.sh:119-137`) excludes guards that cannot be run blind from the local tree. None of these appear in the `scripts-tests (python)` shard, so they are out of scope for the audit's parity index (the issue scoped to that shard explicitly). They are CI-only by construction (need network / DB / npm / arguments) and the umbrella's `SKIP_LIST` is the canonical place to record that. Listed inline rather than as bullets so the AC#1 verify count below matches the in-scope guard count exactly:

`check-issue-author.sh`, `check-duplicate-pr.sh`, `check-shipped-pr.sh`, `check-pr-title.sh` (each needs an issue/PR number argument); `check-blocked-issues.sh` (scans live GitHub issues); `check-task-recovery.sh` (needs a worktree path argument); `check-issue-verify-sql.py` (needs `--issue N` or `--body-file PATH`, see #4372); `check-graphql-queries.sh` (requires `packages/web` npm install); `check-migration-number-collision.sh` and `check-migration-number-collision.py` (need `gh pr list` for cross-PR diff and a `--base` argument); `check_schema_drift.sh` (requires Docker; already wired separately at lines 583-611 with a Docker-availability skip); `check-diff-coverage.sh` (interactive tooling helper, takes a positional `<package>` arg); `check-scraper-zero-record-runner.py`, `check-scraper-zero-record-streak.py`, `check-short-unsubstantive-rulings.py` (scheduled-cron data-quality scripts that run inside ECS against the dev DB).

## Why no follow-up issues are filed

The audit's "for each CI-only check, file a follow-up to wire it to pre-push" output is empty because:

1. **All 26 in-scope guards are already wired** — via `scripts/run-ci-guards.sh` (auto-discovery) plus six standalone gates. The diff `comm -23` shows zero gaps.
2. **The umbrella is self-maintaining** — adding a new `scripts/check-foo.sh` makes it pick up automatically (umbrella discovers any executable matching `scripts/check-*.{sh,py}`, sorts alphabetically, runs them). Test scenario 22 in `scripts/tests/test_pre_push.sh` exercises this. So the recurrence class the audit was meant to prevent — "agent burns full CI round-trip on a one-line miss for a check that's only in CI" — is closed by construction.
3. **The issue's stated upper bound was wrong by construction** — issue #4333 estimated "~5-10 guards in CI not yet in pre-push." Counted from the wrong baseline: at filing time (17:30 UTC), the umbrella in #4332 was filed (17:28 UTC) but not yet merged (18:08 UTC). By the time any /task agent picked this up, parity was already 100%.

## Surrounding context: what other CI jobs invoke check-*

For posterity (out of scope per the issue's threshold which scoped to the `scripts-tests (python)` shard) — there are ~50 additional `check-*` invocations across other CI jobs (lines 1199-2025 of `.github/workflows/ci.yml`). All of them are also covered by the umbrella's runnable list, with the same six exceptions documented in "Built-in skips" above (`check-graphql-queries.sh`, `check-migration-number-collision.sh`, etc.). A full-CI parity audit would produce the same finding: the umbrella has cleaned up the entire bug class.

## Per-guard verification command

To verify the audit's table programmatically:

```
# scripts-tests (python) shard inventory:
grep -nE 'run:' .github/workflows/ci.yml \
  | awk -F: '$1 >= 700 && $1 <= 985' \
  | grep -oE 'check-[a-z0-9-]+\.(sh|py)' | sort -u

# umbrella runnable list:
scripts/run-ci-guards.sh --list 2>&1 | grep '^  RUN ' | awk '{print $2}' | sort

# Diff (must be empty for the audit's "100% parity" claim to hold):
diff <(grep -nE 'run:' .github/workflows/ci.yml \
       | awk -F: '$1 >= 700 && $1 <= 985' \
       | grep -oE 'check-[a-z0-9-]+\.(sh|py)' | sort -u) \
     <(scripts/run-ci-guards.sh --list 2>&1 | grep '^  RUN ' | awk '{print $2}' | sort)
```

A re-run of these commands after future CI / umbrella edits confirms the parity invariant holds. If a new guard is added to `ci.yml` but the umbrella doesn't pick it up (e.g. it lives outside `scripts/check-*` naming, or it lands in the umbrella's SKIP_LIST), the diff will be non-empty — at which point a per-guard follow-up issue is the right output.

## Source-file docstring contradictions

None. The pre-push hook (`.githooks/pre-push`) and `scripts/run-ci-guards.sh` already document their relationship correctly. `.githooks/pre-push:909-921` cites #4332 and `scripts/run-ci-guards.sh` directly; `scripts/run-ci-guards.sh:1-85` documents the auto-discovery rules + `SKIP_LIST` + `# ci-guards: skip` opt-out marker; the standalone gates that predate the umbrella (`check-subprocess-timeouts.sh`, `check-no-unbounded-timeouts.py`, etc.) carry their own #-issue citations.

## References

* Issue #4332 — `feat(dx): scripts/run-ci-guards.sh umbrella + pre-push hook integration` (closed 2026-05-08T18:08Z).
* Issue #4328 — `feat(dx): run subprocess-timeouts check in pre-push hook` (closed 2026-05-08T17:29Z; standalone-gate predecessor for guard #25).
* Issue #4333 — this audit (parent).
* `scripts/run-ci-guards.sh` — umbrella (canonical implementation).
* `.githooks/pre-push:908-939` — umbrella wire-in.
* `scripts/tests/test_pre_push.sh` scenarios 22 and 23 — umbrella + bypass coverage.
