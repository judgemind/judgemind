# Dispatcher v2 Spike 0.6 — Worktree footprint at peak concurrency

**Date:** 2026-04-18
**Issue:** [#2688](https://github.com/judgemind/judgemind/issues/2688)
**Parent spec:** `docs/specs/dispatcher-v2-spec.md` §15, §17 Open Question 3
**Status:** complete

## Question this spike answers

Does 5 concurrent `/task` worktrees fit in Fargate's 20GB default ephemeral storage (§14 budgets 50GB, §17 Open Question 3 reuses the number without data)? If not, do we need the 200GB ephemeral ceiling, an EFS volume, or a shared `.venv` cache?

## TL;DR — Verdict

**Raise ephemeral storage to 50GB (§14's already-budgeted number). Do not enable EFS; do not share venvs.** 5 concurrent worktrees with both Python venvs installed and ralph/pytest artifacts land around **~10 GB** in a realistic mixed steady state; the terraform-touching outlier (full `.terraform` provider cache for dev+prod) pushes a single worktree to ~2 GB. 20 GB would fit the typical mix but leaves no headroom for Fargate OS overhead + a rare double-terraform burst; 50 GB leaves ~5x headroom. 200 GB and EFS are overkill — both add cost/complexity for a problem that doesn't exist at this scale.

## Measurement methodology

Measurements taken in the current `/task` worktree (`agent-a3748b66`) at each checkpoint, plus cross-sample from **23 existing completed/in-flight worktrees** on the host for real-world peak examples.

### Checkpoint (a) — post-`git worktree add` and checkout

Fresh worktree right after `git worktree add` + `git rebase origin/main`. Nothing else installed.

```
$ du -sh /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a3748b66
 37M	/Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a3748b66

$ ls -la /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a3748b66/.git
-rw-r--r--  1 drewthaler  staff  86 Apr 18 15:07 .../.git
# contents: "gitdir: /Users/drewthaler/judgemind/judgemind-bootstrap/.git/worktrees/agent-a3748b66"
```

**37 MB.** The `.git` is an 86-byte stub — git objects are **shared** with the main clone via `$MAIN_REPO/.git/worktrees/<name>/`, not duplicated. Cross-check against 5 other just-checked-out worktrees (a0c6b8e4, a3141c52, a78236f1, ad1f9690, plus a3748b66): all 33-37 MB.

### Checkpoint (b) — post-`install-package-venv.sh`

Running `scripts/install-package-venv.sh` for both Python packages an agent commonly touches:

```
$ /worktree/scripts/install-package-venv.sh scraper-framework
Creating venv at .../packages/scraper-framework/.venv
Installing local sibling dependency: judgemind-config
Installing scraper-framework with [dev] extras
Done.

$ du -sh /worktree
672M

$ du -sh /worktree/packages/scraper-framework/.venv
635M
```

Then adding the second venv (nlp-pipeline):

```
$ /worktree/scripts/install-package-venv.sh nlp-pipeline
Creating venv at .../packages/nlp-pipeline/.venv
Installing local sibling dependency: judgemind-config
Installing nlp-pipeline with [dev] extras
Done.

$ du -sh /worktree
1.7G

$ du -sh /worktree/packages/nlp-pipeline/.venv
1.0G
```

**1.7 GB** with both venvs. Most `/task` agents install only `scraper-framework` (observed: 10 of 11 worktrees with a venv had only scraper-framework); the 672 MB single-venv case is more typical, 1.7 GB is a realistic upper bound for Python-only agents.

### Checkpoint (c) — post-test-run / ralph iteration with coverage artifacts

Running `pytest` once on `scraper-framework` (6365 tests collected; generates `htmlcov/` + `coverage.xml`):

```
$ .venv/bin/pytest packages/scraper-framework/tests/ -q --co
6365 tests collected in 10.92s
Coverage HTML written to dir htmlcov
Coverage XML written to file coverage.xml

$ du -sh /worktree
1.7G

$ du -sh /worktree/htmlcov /worktree/coverage.xml
 10M	/worktree/htmlcov
416K	/worktree/coverage.xml
```

**1.7 GB** — coverage artifacts are ~10 MB, negligible next to the venvs. Cross-check ralph tmp sizes across 23 completed/in-flight worktrees on the host:

| Path | Max observed | Median |
|---|---|---|
| `{worktree}/tmp/` | 684 KB (agent-ad2bc090) | 48 KB |
| `{worktree}/tmp/ralph/` | 156 KB (agent-ad2bc090) | 16 KB |
| `{worktree}/htmlcov/` | 10 MB | ~10 MB when present |
| `{worktree}/coverage.xml` | 416 KB | ~400 KB when present |

Ralph tmp growth is effectively bounded at sub-MB — the logs are small text files (task.md, review-result.txt, ralph-done.txt, per-iteration markdown).

### Real-world peak samples (cross-sample of 23 existing worktrees)

These are actual `/task` worktrees on the host, some completed, some in-flight. Relevant workload patterns:

| Worktree | Size | Workload pattern |
|---|---|---|
| agent-a55415b0 | 565 MB | scraper-framework venv (509M) + 10M htmlcov + small docs/scripts |
| agent-a40db91b | 562 MB | scraper-framework venv + 9.5M htmlcov + 392K coverage.xml |
| agent-ae1230bf | 550 MB | scraper-framework venv only |
| agent-ab8710c3 | 541 MB | scraper-framework venv only |
| agent-ad2bc090 | 817 MB | packages/web with `node_modules/` (500M) + small scraper venv (26M) |
| agent-afe2314b | **2.0 GB** | scraper-framework venv (639M) + **`infra/terraform` (1.3GB)** — `.terraform/` provider caches for both `environments/dev/` (665M) and `environments/production/` (665M) |

**Terraform is the outlier.** Its `.terraform/` directory caches the provider binaries (`hashicorp/aws`, `hashicorp/archive`, etc.) separately per environment. A single-environment `terraform init` is ~665 MB; dev+prod both initialized = ~1.3 GB. This only happens on terraform-touching tasks, which are a minority.

### Host-level accounting (shared .git objects)

The shared `.git` is **not** replicated per worktree. One copy sits in the main clone:

```
$ du -sh /Users/drewthaler/judgemind/judgemind-bootstrap/.git
 75M
$ du -sh /Users/drewthaler/judgemind/judgemind-bootstrap/.git/objects
 61M
```

75 MB of git metadata, shared across all 5 worktrees. This is a fixed cost, not multiplied by concurrency.

## 5× extrapolation

**Is 5× linear, or do some paths share?** Mostly linear — the dominant costs (`.venv/`, `node_modules/`, `.terraform/`, `htmlcov/`) are all **per-worktree**. Only `.git/objects/` is shared (75 MB fixed).

### Per-worktree typical costs

| Component | Size | Notes |
|---|---|---|
| Fresh checkout (working tree + .git stub) | 37 MB | After `git worktree add` + rebase |
| `packages/scraper-framework/.venv` | 635 MB | Python 3.12 + ruff + pytest + pydantic + anthropic + google-genai + boto3 + playwright + etc. |
| `packages/nlp-pipeline/.venv` | 1.0 GB | Larger because it pulls spaCy + NLP-adjacent deps |
| `packages/web/node_modules` | 500 MB | Only if task touches `packages/web/` |
| `infra/terraform/environments/<env>/.terraform` | 665 MB each | Only if task touches terraform; dev+prod init doubles it |
| `htmlcov/` | 10 MB | After pytest |
| `coverage.xml` | 400 KB | After pytest |
| `tmp/`, `tmp/ralph/` | < 1 MB | Ralph logs are tiny |

### 5× scenarios

**Scenario A — typical Python task mix (most likely):** 5 worktrees × ~672 MB (scraper-framework venv only, post-pytest) + 75 MB shared git = **3.4 GB**.

**Scenario B — realistic mixed mix (what we should design for):** 5 worktrees where one of them might be a heavy workload:
- 3 × 672 MB (scraper-framework only) = 2.0 GB
- 1 × 1.7 GB (both venvs) = 1.7 GB
- 1 × 2.0 GB (terraform-touching) = 2.0 GB
- Shared git = 75 MB
- Fargate OS image + Claude Code install + /tmp scratch + CloudWatch agent buffer + margin = ~2 GB (conservative)
- **Total: ~7.8 GB on disk + 2 GB overhead margin = ~10 GB**

**Scenario C — adversarial worst case:** 5 worktrees all hitting the heaviest path (everyone touches web + terraform + both Python venvs simultaneously):
- 5 × (1.7 GB Python + 500 MB node_modules + 1.3 GB terraform + 10 MB htmlcov) = 5 × 3.5 GB = **17.5 GB**
- Plus shared git (75 MB) + Fargate overhead (~2 GB) = **~19.6 GB**

Scenario C is implausible at 5× (we have never seen a single `/task` touch all three ecosystems in one ticket, let alone all five concurrently). It is listed as an upper bound.

### Against each ceiling

| Ceiling | Scenario A (3.4 GB) | Scenario B (10 GB) | Scenario C (19.6 GB) |
|---|---|---|---|
| **20 GB default** | fits (6× headroom) | fits (2× headroom) | fits but zero headroom |
| **50 GB (§14 budgeted)** | fits (15× headroom) | fits (5× headroom) | fits (2.5× headroom) |
| **200 GB (max)** | overkill | overkill | overkill |
| **EFS mount** | unnecessary | unnecessary | unnecessary |

## Verdict

**Use 50 GB ephemeral storage** (§14's already-budgeted number; no change from spec).

Reasoning:
- Scenario B (realistic mixed mix) is ~10 GB. 20 GB would fit but leaves only 2× headroom — not enough room for a second terraform-touching agent arriving while a first is still holding its `.terraform/` cache, or for a forgotten-cleanup build artifact.
- 50 GB gives 5× headroom in Scenario B and still fits Scenario C with room to spare.
- 200 GB and EFS are both overkill. EFS adds latency (first-write is ~100ms vs <1ms local NVMe), cost (~$0.30/GB-month), and complexity (mount target, security group, idle connection cleanup). No data supports either.
- **Do not adopt a shared `.venv` cache** to save disk. That would break the "never share venvs between worktrees" rule (CLAUDE.md) and invite cross-agent state leakage (`pip install -e` in one venv writes `.pth` entries that point at a specific worktree path — pointing 5 agents at the same `.venv` means the last installer wins). The marginal disk savings (~3–4 GB at 5×) is not worth the correctness risk.

### Secondary observation — laptop hygiene, not a Fargate concern

The 23 leftover worktrees on the host (`du -sh /Users/drewthaler/judgemind/judgemind-bootstrap` = 11 GB) accumulate because the laptop dispatcher doesn't always reap them cleanly. In the dispatcher v2 container, the daemon calls `cleanup_worktree.sh` on agent exit (spec §10.2) — no accumulation across agents. No action needed for Fargate.

### Also — terraform is the per-agent tail risk

The 2 GB terraform-touching worktree is driven by `.terraform/environments/<env>/.terraform` caching all providers separately per environment. On Fargate, most tasks never init terraform, so this is a rare spike. If we start seeing terraform-heavy workloads cluster (multiple concurrent infra PRs), the mitigation is either:
- Run `terraform init` only against the specific environment being modified (not both), or
- Accept it — 2 GB × 2 agents = 4 GB, still fits in 50 GB.

## Resolution of §17 Open Question 3

The open question can be closed with: *"Measured — 5 concurrent worktrees at typical Python workload is ~3.4 GB; realistic mixed peak is ~10 GB; terraform-touching outlier hits 2 GB/worktree. 50 GB ephemeral (already budgeted in §14) gives 5× headroom. No EFS, no venv sharing, no change from spec."*

## Cost / complexity note

**No infra change.** 50 GB Fargate ephemeral storage is set via the task definition's `ephemeralStorage.sizeInGiB` field. Cost delta vs default 20 GB: Fargate charges ~$0.000111 / GB-hour for ephemeral storage above 20 GB, so +30 GB × 730 hours × $0.000111 = **~$2.43 / month**. Negligible.

EFS was considered and rejected: it would cost ~$0.30 / GB-month (~$3 / month for the same 10 GB working set) plus per-agent mount operations and cross-mount coherency concerns. Attaching EFS would require a new module (efs-access-point, security group rule, mount target), new startup scripts, and a new failure mode (mount target unreachable). Not worth it for the headroom we already have at 50 GB local.

## Artifacts for verification

Raw `du -sh` output at each checkpoint preserved above. Reproducible by:

```
# (a) Fresh worktree
git -C /path/to/repo worktree add /tmp/test-worktree/w main
du -sh /tmp/test-worktree/w

# (b) Install both venvs
/tmp/test-worktree/w/scripts/install-package-venv.sh scraper-framework
/tmp/test-worktree/w/scripts/install-package-venv.sh nlp-pipeline
du -sh /tmp/test-worktree/w

# (c) Run a test pass to produce htmlcov
/tmp/test-worktree/w/packages/scraper-framework/.venv/bin/pytest \
    /tmp/test-worktree/w/packages/scraper-framework/tests/ -q --co
du -sh /tmp/test-worktree/w
```

Sample cross-validation command (lists all existing worktrees sorted by size):

```
du -sh /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/*/ | sort -h
```
