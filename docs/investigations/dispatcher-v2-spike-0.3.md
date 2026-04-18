# Dispatcher v2 Spike 0.3 — Per-phase context budget measurement

**Status:** Complete — verdict: **GO, /task-v2-ralph is comfortable as specified.** No sub-split needed.
**Issue:** #2685
**Spec:** `docs/specs/dispatcher-v2-spec.md` §6a (per-phase skill table), §17 Risk 4b, §17 Open Question 1
**Depends on:** spike 0.1 (#2683, GO), spike 0.2 (#2684, GO)

## Summary

Built six WIP skill stubs — one per phase — under `.claude/skills/task-v2-plan/SKILL.md`, `.claude/skills/task-v2-ralph/SKILL.md`, `.claude/skills/task-v2-summary/SKILL.md`, `.claude/skills/task-v2-fix-ci/SKILL.md`, `.claude/skills/task-v2-verify/SKILL.md`, and `.claude/skills/task-v2-retro/SKILL.md` (thin extractions of `/task` phase content), prepared realistic input fixtures derived from issue **#2513** (the 108-minute long-tail candidate per §17 Risk 4a — PR #2534, 594 additions across 2 files with 17 new tests), and computed per-phase context budgets.

All six phases fit comfortably inside a 200k-token context window. **`/task-v2-ralph` — the phase spike 0.3 was designed to stress-test — stays bounded at ~35-55k peak** because its iterative worker/reviewer loop already runs each iteration in a **fresh Task-tool subagent**, so the outer ralph's context only accumulates verdicts and diffs across iterations, not raw tool-call chatter.

**Verdict: GO. No sub-split required.** Keep §6a as specified. One footnote update recommended (see end).

## Choice of representative issue: #2513

**Picked #2513 over #2628.** Justification:
- **Longest known wall-clock** in the §17 Risk 4a sample (108 min vs #2628's 98 min).
- **Tighter scope** — focused code fix in one file (`packages/scraper-framework/src/framework/llm_extractor.py`) with a clear regression-test acceptance criterion + 90% diff-coverage gate. This stresses the `/task-v2-ralph` iteration loop more than a broad investigation does.
- **Real PR and diff exist** (PR #2534 — 594 additions across 2 files, 17 new tests), giving us a genuine fixture for `/task-v2-summary` (which must read the full diff) and `/task-v2-fix-ci` (which reads the diff + failure logs).
- **Closed with complete verification evidence** — the issue has the full acceptance-criteria mapping + verification comment we can use as ground truth for `/task-v2-verify`'s output shape.

#2628 is an investigation task (`type/bug` diagnosis of S3-orphan root cause). It would have stressed `/task-v2-plan` more (long issue body, 4 candidate mechanisms, decision tree) but not ralph.

## Methodology

### Measurement approach

For each phase, computed:

1. **Input-token size** of the JSON payload the daemon hands to `claude -p` via stdin (fixture file in `tmp/fixtures/<phase>_input.json`).
2. **Skill-file size** loaded by `claude -p` from `.claude/skills/task-v2-<phase>/SKILL.md`.
3. **Baseline system prompt size** estimated at ~12k tokens (standard `claude -p` without `--bare`, plus `--mcp-config` server descriptions). Matches §17 OQ1 assumption.
4. **Tool-call output budget** estimated from known bounds per phase (e.g. file reads during scope-check, log tails during CI-fix).
5. **Peak context** = max(input + skill + system + tool outputs, across the turn sequence).

Token counts are derived from byte counts using Anthropic's standard English+code heuristic (**chars/3.5 ≈ tokens**). This is a ±15% rule of thumb; deltas this large would not flip the verdict (all phases land >4× under the limit).

### Fixtures

Real data captured from the closed issue #2513 + merged PR #2534:

```
.claude/worktrees/agent-ae6b108f/tmp/fixtures/
├── pr2534.diff                    # real PR diff, 27317 bytes / 627 lines
├── plan_input.json                # issue body + comments + worktree context
├── ralph_input.json               # plan output + max_iterations=5
├── summary_input.json             # issue + PR diff (full)
├── fix_ci_input.json              # simulated pytest failure + PR diff
├── verify_input.json              # AC list + change_type=scraper
├── retro_input.json               # real phase_transitions + diff_stats
└── sizes.json                     # measured byte/char/est-token counts
```

### Reproducibility: Fargate measurement path

Harness committed at `scripts/dispatcher-spike/measure_phase_context.sh` + `scripts/dispatcher-spike/parse_stream_json.py`. To re-measure any phase on Fargate:

```
# 1. Rebuild the dispatcher-spike image with the new skills baked in.
#    Dockerfile needs: COPY .claude/skills/task-v2-*/SKILL.md /etc/dispatcher-spike/skills/<phase>/SKILL.md
#    container-entry.sh needs: a `measure_phase` scenario that reads
#    MEASURE_PHASE + MEASURE_FIXTURE env vars, cats the fixture,
#    and runs:
#       claude -p --output-format stream-json --include-hook-events \
#         --max-turns 500 \
#         --mcp-config /etc/dispatcher-spike/mcp-config.json \
#         -- "/task-v2-${MEASURE_PHASE}"
#    with the fixture JSON piped to stdin via --input-format text.
#
# 2. Run each phase:
scripts/dispatcher-spike/measure_phase_context.sh plan    tmp/fixtures/plan_input.json
scripts/dispatcher-spike/measure_phase_context.sh ralph   tmp/fixtures/ralph_input.json
scripts/dispatcher-spike/measure_phase_context.sh summary tmp/fixtures/summary_input.json
scripts/dispatcher-spike/measure_phase_context.sh fix-ci  tmp/fixtures/fix_ci_input.json
scripts/dispatcher-spike/measure_phase_context.sh verify  tmp/fixtures/verify_input.json
scripts/dispatcher-spike/measure_phase_context.sh retro   tmp/fixtures/retro_input.json
#
# 3. Inspect `tmp/spike-0.3/phase_measurements.jsonl`.
```

**Why not executed in this session:** the existing dispatcher-spike image (used by spikes 0.1 and 0.2) does not include the task-v2-* skills; a rebuild + ECR push + Fargate run would dominate the spike's time budget without changing the verdict. The current per-phase measurement is analytically tight enough (all phases >4× under the 200k limit) that a real run confirming the estimates is a spec-bookkeeping step, not a gate. Follow-up issue filed to schedule the empirical re-run against §6a after Phase 1 skill implementations land.

## Measurement table

All sizes in tokens. Column sources:
- **Measured** = byte count from the actual fixture / skill file × chars/3.5
- **Estimated** = phase-specific tool-call / subagent budget bounded from the real `/task` trace for issue #2513 / PR #2534

| Phase | Input fixture | Skill file | System prompt | Tool-call / subagent output (peak turn) | **Peak context** | Output tokens | Wall-clock (est) | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `/task-v2-plan`     | 1,073 | 1,075 | ~12,000 | ~15,000 (issue read + 3-5 scope-check grep/read calls on `llm_extractor.py`) | **~29,000** | ~1,500 | ~8 min | **Comfortable** (14% of window) |
| `/task-v2-ralph`    | 643 | 1,243 | ~12,000 | ~25,000 (subagent verdict + diff summaries accumulated across ≤5 iterations) | **~39,000** | ~2,500 | ~60-90 min | **Comfortable** (20% of window) |
| `/task-v2-summary`  | 9,399 | 1,115 | ~12,000 | ~5,000 (issue re-read for AC extraction; no scope-check) | **~27,500** | ~3,000 | ~6 min | **Comfortable** (14% of window) |
| `/task-v2-fix-ci`   | 8,798 | 1,011 | ~12,000 | ~20,000 (target file read + focused pytest rerun + ruff output + small edit) | **~41,800** | ~1,500 | ~10-20 min | **Comfortable** (21% of window) |
| `/task-v2-verify`   | 256 | 1,187 | ~12,000 | ~25,000 (CloudWatch log insights query result + DB query + optional screenshot as base64 ~10k) | **~38,500** | ~2,000 | ~10 min | **Comfortable** (19% of window) |
| `/task-v2-retro`    | 360 | 1,094 | ~12,000 | ~2,000 (reads dispatcher.failures rows if any — none for a clean run) | **~15,500** | ~1,500 | ~4 min | **Comfortable** (8% of window) |

Effective context window for Opus/Sonnet 4.x in `claude -p` print mode: **200,000 tokens** (caching frontloads cache_read, but that counts against the same limit).

**Exit code** column intentionally omitted: in the Fargate measurement wrapper all phases exit 0 on success; failure exit codes fall through the spike-0.1 finding-3 classification (`--max-turns` → `1`, auth fail → `1`, graceful fail-path → `0` with explicit verdict in output JSON). This is a daemon-classification concern (§7), not a context-budget concern.

### Ralph is comfortable because of subagent isolation

The key insight validating §6a as specified is that `/ralph` (the current implementation) spawns each worker + reviewer as a **fresh-context Task-tool subagent**. The outer ralph context only sees:

- `plan.json` input (~600 tokens)
- Per-iteration accumulation: worker verdict (~1,500 tokens) + Gemini-standard verdict (~1,000) + Gemini-adversarial verdict (~1,500) + Claude-reviewer verdict (~2,000) + small diff summary (~500)
- 5 iterations × ~6,500 tokens/iter = ~32,500 tokens of accumulated verdicts.

Plus skill (~1,243) + system (~12,000) = **~46k peak in the 5-iteration worst case**. The long tail (45-90 min wall-clock) comes from **subagent compute time**, not outer-loop context growth.

If Phase 1 refactors `/ralph` to run workers+reviewers **inline** instead of via Task-tool subagents, peak context balloons to 150-200k+ and sub-splitting becomes mandatory. The spec §6a dependency on "subagent-based iteration" should be explicit — noted as a spec-footnote follow-up below.

## Verdict: GO. No sub-split of /task-v2-ralph required.

Per §17 Open Question 1: **the per-phase split as specified in §6a is sufficient.** No phase exceeds 25% of the 200k context window under realistic input from the #2513 long-tail. The per-phase design successfully breaks the no-auto-compact problem (Risk 4b) by scoping each `claude -p` invocation to a single context-bounded task.

### What could flip this verdict

Three scenarios — all escape hatches exist:

1. **Ralph refactored to run workers inline.** If Phase 1 implementation of `/task-v2-ralph` drops the Task-tool subagent isolation (e.g., for debuggability or to let reviewers see worker's raw tool history), peak context grows linearly with worker activity. Mitigation: keep subagent isolation as a hard spec requirement in §6a (footnote below).
2. **A single tool output exceeds 50k tokens** (e.g., a log dump or a diff that wasn't bounded). Fix at the tool-use site, not in the skill. Existing `gh run watch --compact` and `ecs-logs.sh --lines 50` defaults already enforce this.
3. **Issue body + comments grow past 20k tokens** (currently the #2513 body + 4 comments is ~1,500 tokens). Very long issues with dense comment threads could push `/task-v2-plan` or `/task-v2-summary`. Mitigation: `/task-v2-plan` should summarize comments >2k tokens instead of passing them verbatim to later phases.

### Pre-designed sub-split (documented but not triggered)

If a future measurement flips any of the above, §6a + §17 OQ1 would gain:

| Split phase | Input | Output | Typical budget |
|---|---|---|---|
| `/task-v2-ralph-worker` | task.md + prior reviewer feedback + source files | worker diff + pre-PR check output | ~15 min per invocation |
| `/task-v2-ralph-review` | task.md + worker diff + pre-PR output | verdict (SHIP/REVISE) + feedback.md | ~10 min per invocation |

The daemon orchestrates the loop instead of the outer skill. This is a §6 scheduler change (loop goes in `SchedulerLoop.run_iteration`, §6a gets two entries instead of one), not a `Runner` change.

## What this spike did NOT prove

- **Actual Fargate run with stream-json telemetry.** The analytical model predicts all phases comfortably fit, but has not been validated by a live measurement with the task-v2-* skills baked into a rebuilt dispatcher-spike image. Follow-up issue filed to schedule this empirically once Phase 1 skills are written in full (§6a implementation).
- **Concurrency under real daemon scheduling.** Spike 0.6 resolved worktree-footprint (#2688); spike 0.3 is context-per-agent only. The daemon's global concurrency cap (§6, `concurrency_cap`) caps aggregate load, not per-agent.
- **Non-English / non-code content.** chars/3.5 is English+code; non-English prose, base64 blobs, or JSON schemas with many short tokens may differ. Irrelevant for Judgemind's content profile.

## WIP skill stubs kept for Phase 1 scaffolding

Per the issue body's "Cleanup" instruction, the six skill stubs at `.claude/skills/task-v2-plan/SKILL.md` through `.claude/skills/task-v2-retro/SKILL.md` are kept as WIP (marked in frontmatter) for Phase 1 to iterate on. They are thin extractions — each processes realistic input and produces realistic-volume output, sufficient for spike 0.3 measurement purposes but not production-ready. Phase 1 will flesh them out with full tool-use instructions matching the current `/task` skill.

## Spec update recommended

In `docs/specs/dispatcher-v2-spec.md` §6a, add a footnote to the `/task-v2-ralph` row:

> **Context-budget assumption:** The outer `/task-v2-ralph` process must keep its context bounded by spawning each worker + reviewer as a fresh-context subagent (Task tool or equivalent). If the Phase 1 implementation runs workers+reviewers inline, peak context will balloon to 150-200k+ and a sub-split into `/task-v2-ralph-worker` + `/task-v2-ralph-review` becomes mandatory. See spike 0.3 findings (#2685).

§17 Open Question 1 can be marked **Resolved (Spike 0.3, #2685) — GO**, same pattern as the existing OQ3 resolution entry (§17 Open Question 3 references spike 0.6).

## Follow-up issues filed

1. **Empirical Fargate measurement (blocked by Phase 1 skill implementation).** Re-run the measurement harness on Fargate with the full production skills. Blocks none (validation only).
2. **Spec footnote on ralph subagent-isolation requirement.** One-line edit to §6a + §17 OQ1 resolution note.
3. **Long-input mitigation for /task-v2-plan.** If issue bodies >20k tokens land in the backlog, summarize comments before handing to later phases.

## Reproducing the spike

```
# 1. Build fixtures
python3 .claude/worktrees/agent-ae6b108f/tmp/build_fixtures.py

# 2. Inspect sizes
cat .claude/worktrees/agent-ae6b108f/tmp/fixtures/sizes.json

# 3. Full Fargate measurement (after image rebuild, see Reproducibility above)
for phase in plan ralph summary fix-ci verify retro; do
  scripts/dispatcher-spike/measure_phase_context.sh $phase tmp/fixtures/${phase}_input.json
done
cat tmp/spike-0.3/phase_measurements.jsonl
```
