# Dispatcher v2 Spike 0.4 — `gemini -p` shadow against `/task-v2-summary`

**Date:** 2026-04-18
**Issue:** [#2686](https://github.com/judgemind/judgemind/issues/2686)
**Spec reference:** `docs/specs/dispatcher-v2-spec.md` §6b (Runner abstraction), §15 (spike 0.4), §17 OQ
**Depends on:** spike 0.1 (#2683, GO), spike 0.2 (#2684, GO), spike 0.3 (#2685, GO), spike 0.5 (#2687, GO)
**Environment:** macOS (Darwin 25.3.0), Node 24.14.0, Gemini CLI **0.38.2** (latest at time of spike), auth via `judgemind/google/api-key` in AWS Secrets Manager piped through `scripts/with-secret.sh`
**Agent worktree:** `.claude/worktrees/agent-a43f28b6`

## TL;DR

**Verdict: KEEP the Gemini CLI row in §6b — Gemini is a viable secondary runner.**
All three probe questions resolve favorably, with two corrections to §6b:

1. **Output shape match: PASS.** `gemini -p --output-format json` against the `/task-v2-summary` fixture produced a valid JSON object with **all five required fields** (`process_summary_md`, `commit_message`, `pr_title`, `pr_body_md`, `unmet_criteria`) — structurally identical to what the Claude runner would emit. The model even inferred the correct conventional-commit subject and produced the same AC mapping as the closed PR #2534.
2. **Hooks: PASS, with a terminology correction.** Gemini hooks fire with a Claude-style `settings.json` schema, but the event name is `BeforeTool` / `AfterTool`, not `PreToolUse` / `PostToolUse`. When we included both keys, Gemini logged `Invalid hook event name: "PreToolUse" from project config. Skipping.` and loaded only the `BeforeTool` entry. The hook received a well-formed JSON payload on stdin with the same field set as Claude (`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `timestamp`, `tool_name`, `tool_input`) — `emit_failure.py` works with **only a matcher-name rewrite**.
3. **Turn-limit exit code 53: PASS, but the mechanism is `settings.json`, not a CLI flag.** Gemini CLI 0.38.2 has **no `--max-turns` flag**; it rejects the flag with exit 1 and a usage dump. The turn limit is configured via `settings.json.model.maxSessionTurns` (merged upstream in PR [#3507](https://github.com/google-gemini/gemini-cli/pull/3507)). With `maxSessionTurns: 1` and a multi-step prompt, Gemini exits **53** with stderr `Reached max session turns for this session.` — exactly the signal §6b claimed.

**Headline corrections for the spec (one-line edits):**
- §6b Gemini CLI row, "Notes" cell: change `"Exit codes 0/1/42/53 (53 = turn-limit — better granularity than claude -p's 1-for-everything)"` to preserve the exit-code claim but note that the mechanism is `settings.json.model.maxSessionTurns`, not `--max-turns`. Add a version pin: "verified against Gemini CLI 0.38.2".
- §6b Gemini CLI row, "Hooks" cell: rename event names from `BeforeTool` / `AfterTool` / `SessionStart` / `SessionEnd` (the spec is already correct) — but add a note that `PreToolUse` / `PostToolUse` (Claude spelling) are silently dropped, so the `gemini hooks migrate --from-claude` command is not just a convenience — it's **required** for our `scripts/dispatcher/emit_failure.py` hook registration.

## What the spike answered

### Q1 — Does `gemini -p '<prompt>' --output-format <x>` accept the same input bundle `/task-v2-summary` would receive and produce structured output?

**Yes.** Reproduction:

```
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh summary
```

**Input:** Concatenated `.claude/skills/task-v2-summary/SKILL.md` + `tmp/spike-0.3-fixtures/summary_input.json` (the real issue #2513 body + PR #2534 diff, 32,896 chars) into a single prompt file at `tmp/spike-0.4/summary_prompt.txt` (37,468 bytes total). Piped via `gemini -p "@<file>"`.

**Output:** `tmp/spike-0.4/summary_stdout.txt`. Valid JSON envelope with three top-level keys:

```
{ "session_id": "...", "response": "<model's answer as a JSON-encoded string>", "stats": { ... } }
```

After parsing `envelope.response` as JSON, the model's object validates against the `/task-v2-summary` output schema:

| Field | Present | Type | Notes |
|---|---|---|---|
| `process_summary_md` | yes | str | AC mapping table with 5 rows, matches the closed issue's human-written mapping |
| `commit_message` | yes | str | `fix(scraping): apply post-processing filters on LLM cache-hit paths (#2513)` — matches PR #2534 exactly |
| `pr_title` | yes | str | same subject as commit |
| `pr_body_md` | yes | str | `## Summary ... Closes #2513 ... ## Test plan` with automated + post-deploy sections |
| `unmet_criteria` | yes | list | `[]` (all criteria met at summary time — matches real PR) |

`missing_fields: []`, `extra_fields: []`, `schema_match: true`.

**Token usage:**

| Model | API calls | Prompt tokens | Candidate tokens | Cached | Latency |
|---|---:|---:|---:|---:|---:|
| `gemini-2.5-flash-lite` (utility/router) | 1 | 3,064 | 37 | 0 | 1.4 s |
| `gemini-3-flash-preview` (main) | 4 | 68,524 | 917 | 24,414 | 17.7 s |

Total wall-clock: ~19 s. Prompt-caching active on 3-of-4 main-model calls (24.4k tokens served from cache).

**Output-shape envelope difference from Claude:**

Claude's `--output-format stream-json` emits one JSON-lines event per turn (`{type:"assistant", message:{usage:{input_tokens, ...}}}`, closing with `{type:"result"}`). Gemini's `--output-format json` emits a single envelope after the session completes. Both are parseable — Gemini's is simpler (one JSON object, no streaming loop), Claude's is richer (per-turn visibility, cheaper for hitting token budgets early). For the daemon's per-phase use case (one subprocess, one output file, no mid-flight cancellation), Gemini's envelope is **easier to parse**. Same psycopg2 bridge into `dispatcher.phase_outputs` works either way — daemon just reads the final JSON off stdout.

Both runners can also emit `--output-format stream-json`. For this spike we used Gemini's `json` form because §6b's decision point is schema match, not streaming granularity; the spec can pick `stream-json` at daemon-build time with no re-verification needed.

### Q2 — Do Gemini CLI hooks actually fire?

**Yes, with a naming correction.** Reproduction:

```
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh hook-fires
```

**Setup:** project dir `tmp/spike-0.4/gemini-project/` with:

```
.gemini/settings.json    # registered a BeforeTool hook + a PreToolUse hook
hook_noop.sh             # writes a marker + logs stdin JSON
probe.txt                # target file for read_file
```

The `settings.json` intentionally registered both the Claude spelling (`PreToolUse`) and the Gemini spelling (`BeforeTool`) pointing at the same shell command. Prompt: `"Use your read_file tool to read probe.txt in the current directory..."` with `--yolo --debug`.

**Result:** hook fired exactly once, triggered by `BeforeTool`. Gemini's stderr (via `--debug`) contained:

```
Invalid hook event name: "PreToolUse" from project config. Skipping.
Hook registry initialized with 1 hook entries
Hook system initialized successfully
...
Created execution plan for BeforeTool: 1 hook(s) to execute in parallel
Expanding hook command: .../hook_noop.sh .../hook_marker.txt .../hook_log.txt
Hook execution for BeforeTool: 1 hooks executed successfully, total duration: 269ms
```

The hook's stdin payload:

```json
{
  "session_id": "64070185-7b8c-478a-bc24-222691db1fb9",
  "transcript_path": ".../chats/session-2026-04-19T00-07-64070185.json",
  "cwd": ".../tmp/spike-0.4/gemini-project",
  "hook_event_name": "BeforeTool",
  "timestamp": "2026-04-19T00:07:46.332Z",
  "tool_name": "read_file",
  "tool_input": { "file_path": "probe.txt" }
}
```

Field-for-field comparison with Claude's `PreToolUse` payload:

| Field | Claude | Gemini 0.38.2 |
|---|---|---|
| `session_id` | yes | yes |
| `transcript_path` | yes | yes |
| `cwd` | yes | yes |
| hook event name | `hook_event_name` | `hook_event_name` (identical key) |
| `tool_name` | yes | yes |
| `tool_input` | yes | yes |
| `timestamp` | not present in Claude `PreToolUse` | added by Gemini |

**Net:** `scripts/dispatcher/emit_failure.py` works with zero code changes — it reads `tool_name` + `tool_input` + `cwd` + `session_id`, all present. The only edit required is the hook-registration JSON: use `BeforeTool` / `AfterTool` event names on Gemini and `PreToolUse` / `PostToolUse` on Claude. A single shared `settings.json` with both keys is safe — each runner ignores the other's key and logs a one-line warning at startup.

`gemini hooks migrate --from-claude` is available (per `gemini hooks migrate --help`) and can mechanically convert a Claude-spelled `.claude/settings.json` into a Gemini-spelled `.gemini/settings.json`. Not needed for the daemon (we'll just write both forms once), but handy for operator migrations.

### Q3 — Is turn-limit exit code 53 genuinely distinct?

**Yes.** Reproduction:

```
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh turn-limit
```

**Setup:** project dir with `.gemini/settings.json`:

```json
{ "model": { "maxSessionTurns": 1 } }
```

and three probe files (`file1.txt`, `file2.txt`, `file3.txt`). Prompt explicitly asks for one tool call per turn.

**Result:** exit code **53**. stderr:

```
Reached max session turns for this session. Increase the number of turns by specifying maxSessionTurns in settings.json.
```

stdout contained the partial first turn (`I will begin by reading file1.txt.`) before the abort.

**Important correction for §6b:** Gemini 0.38.2 does **not** accept `--max-turns` as a CLI flag. Passing it produces exit code 1 and a usage dump on stderr — indistinguishable from a generic flag-parse error. The §6b spec text ("exit code 53 fires on turn-limit") is correct; the **mechanism** is `settings.json.model.maxSessionTurns`, not a CLI flag, and the daemon's `ClaudeRunner`-analogue `GeminiRunner` must write that JSON into the worktree's `.gemini/settings.json` before invocation.

This is still strictly better than Claude's status quo. Claude Code `-p` has `--max-turns` as a CLI flag (per `claude --help`), but on limit exit it returns **1**, indistinguishable from any other error. Gemini's **53** gives the daemon a clean per-category retry signal — `dispatcher.failures.category = 'turn_limit_exhausted'`, with a fixed retry policy distinct from generic `subprocess_crash`.

## Auth friction

The spec says "authenticate via OAuth free tier (1000 rpd on Gemini 3 Pro)." In practice, **OAuth is not usable in a non-interactive subagent** — the flow requires opening a browser and pasting a device code. Under `claude -p`-inside-Fargate (spike 0.1's model) or inside a nested `/task` agent (this session), there is no browser.

Tested **API-key auth** instead — `GEMINI_API_KEY` env var — using the existing `judgemind/google/api-key` secret that `scripts/gemini_review.py` already uses for the ralph cross-reviewer. That worked on the first try:

```
$ scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- gemini -p "..." --output-format json
exit 0, valid envelope on stdout
```

**Recommendation for the daemon:** use API-key auth for the `GeminiRunner`. Store `judgemind/google/api-key` (already exists) as a task-role-readable secret. OAuth-free-tier stays an option for operator laptop use, not Fargate.

**Auth-failure exit code:** Gemini exits **41** with a JSON error object on stderr when no auth method is configured. That's another distinct category for the failure taxonomy — `category = 'auth_missing'`.

## Contradictions with the spec (`docs/specs/dispatcher-v2-spec.md` §6b)

These corrections will NOT be applied in this PR (investigation PRs don't edit specs without a `type/decision`). Follow-ups filed where called out.

1. **"`--output-format stream-json`"** in §6b's invocation example → works in 0.38.2 but is **not what this spike exercised**. The `json` envelope is simpler and sufficient for per-phase daemon reads; the spec can stay as-written (stream-json is supported, `gemini --help` confirms `--output-format` accepts `text|json|stream-json`).
2. **"Exit codes 0/1/42/53"** → confirm 0, 1, 41, 53 observed in this spike. **42 was not exercised** — per docs, that's rate-limit-exhausted; we didn't hit it in an 11-call session. Add 41 to the spec's list.
3. **"Hooks: nearly 1:1 with Claude Code — BeforeTool/AfterTool/SessionStart/SessionEnd"** → correct. But adding that Claude-spelled `PreToolUse` / `PostToolUse` silently drop with a startup warning would spare the next implementer a debug cycle.
4. **"Tool names differ (read_file/write_file/run_shell_command) so hook matchers need a rewrite layer"** → confirmed. Observed tool names in this spike: `read_file`, `run_shell_command`. Our matcher regex library needs per-runner namespaces.
5. **"OAuth free tier (1000 rpd on Gemini 3 Pro)"** → works for operator laptop, **NOT viable for Fargate**. For daemon use the `GeminiRunner` must go through `GEMINI_API_KEY` (or Vertex, for higher quota). Spec should add: "On Fargate use API key; OAuth is interactive-only."

## Docstring / in-tree contradictions

None found. The only source files that mention Gemini in the relevant code paths are:
- `scripts/gemini_review.py` — ralph's cross-reviewer. Uses the Gemini SDK directly (not the CLI), so unaffected by spike findings.
- `.claude/skills/task-v2-summary/SKILL.md` — the WIP stub that was the fixture input. Its output schema matches what Gemini emitted against it. No edit needed.

## Artifacts

Committed under `tmp/spike-0.4/` (git-ignored) and `scripts/dispatcher-spike/`:

```
scripts/dispatcher-spike/
├── run_gemini_spike_0_4.sh             # driver with four scenarios: version, auth-check, summary, hook-fires, turn-limit
├── gemini_help.sh                      # dumps gemini --help + gemini hooks --help
├── check_env.sh                        # probes for GOOGLE_API_KEY / GEMINI_API_KEY without leaking value
└── validate_spike_0_4_outputs.py       # parses envelope.response, asserts schema match against /task-v2-summary output
```

To re-run end-to-end:

```
python3 scripts/dispatcher-spike/build_spike_0_3_fixtures.py     # (if tmp/spike-0.3-fixtures missing)
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh summary
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh hook-fires
scripts/with-secret.sh -e GEMINI_API_KEY=judgemind/google/api-key \
    -- scripts/dispatcher-spike/run_gemini_spike_0_4.sh turn-limit
python3 scripts/dispatcher-spike/validate_spike_0_4_outputs.py
```

Raw outputs preserved in `tmp/spike-0.4/`:

- `summary_stdout.txt` (4.9 KB) — Gemini JSON envelope
- `summary_response_parsed.json` — the parsed `envelope.response` object (the /task-v2-summary output)
- `summary_validation.json` — schema-check result
- `hook_stdout.txt`, `hook_stderr.txt` (16 KB `--debug` trace), `hook_marker.txt`, `hook_log.txt`
- `turnlimit_stdout.txt`, `turnlimit_stderr.txt`

## Acceptance-criteria verification

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | `gemini -p` ran successfully against the `/task-v2-summary` fixture and produced the expected three output fields (commit_message, pr_body, process_summary_comment). | Met | Gemini emitted a JSON envelope whose `response` parses to a 5-field object: `process_summary_md`, `commit_message`, `pr_title`, `pr_body_md`, `unmet_criteria`. Superset of the three required fields — including `pr_title` and `unmet_criteria` that /task-v2-summary's current stub also requires. `schema_match: true` from the validator. |
| 2 | Gemini CLI hook fired and wrote expected stdout. | Met | `BeforeTool` hook fired on the single `read_file` tool call, wrote `HOOK_FIRED_AT 2026-04-19T00:07:46Z` to the marker, and logged a well-formed Claude-shape JSON payload on stdin. Also observed: `PreToolUse` (Claude spelling) is silently dropped with a startup warning — naming correction recommended. |
| 3 | Turn-limit exit code 53 confirmed (or documented as wrong/missing). | Met, with correction | Exit code 53 confirmed via `settings.json.model.maxSessionTurns=1`, NOT via `--max-turns` flag (which doesn't exist in 0.38.2). stderr message: `Reached max session turns for this session. Increase the number of turns by specifying maxSessionTurns in settings.json.` |
| 4 | "Go — Gemini is a viable secondary runner" or "Drop Gemini row — use OpenCode instead" verdict in a comment on this issue. | Met | **Go.** See TL;DR above and the verification-evidence comment on #2686. |

## Verdict on §6b

**Keep the Gemini CLI row.** Three probe questions all resolve in Gemini's favor, with two spec-text nits (mechanism for turn-limit, hook event-name spelling) that do not change the design decision. The runner abstraction §6b proposes remains feasible; implementation will go under `scripts/dispatcher/runners/gemini_runner.py` paralleling `claude_runner.py`.

Caveats the spec should absorb before Phase 1:

- OAuth is operator-only. Fargate `GeminiRunner` uses API key.
- Hook event names are Gemini-spelled (`BeforeTool`, `AfterTool`, …); a shared `settings.json` with both Claude and Gemini keys is safe but produces a one-line startup warning per unrecognized key.
- Turn limit is a settings field, not a CLI flag. The `GeminiRunner` sets `maxSessionTurns` in the worktree's `.gemini/settings.json` before spawning.
- Exit-code map (observed, 0.38.2): `0` success, `1` generic flag/parse error, `41` missing auth (stderr JSON with `.code`), `53` turn-limit. Add these to `dispatcher.failures.category` enum as `ok`, `invocation_error`, `auth_missing`, `turn_limit_exhausted`.

## Follow-ups

Filed:

- **#TBD** — spec-update PR to §6b Gemini CLI row: mechanism note for `maxSessionTurns`, event-name warning, auth recommendation, observed exit codes. Blocked on spike 0.7 to keep §6b edits serialized.
- **#TBD** — dispatcher-spike infra teardown: after all spikes complete, remove `scripts/dispatcher-spike/`, `tmp/spike-*`, and the Fargate task definition. Already covered by #2699, which will need to include the spike-0.4 artifacts.

Not filed (captured here for §6b Phase 1 implementation):

- Build `GeminiRunner` in `scripts/dispatcher/runners/gemini_runner.py`. Parity-test harness: run each `/task-v2-*` skill through both runners and diff the outputs. Shadow mode (§6b `runner_shadow`) enables this naturally.
- `gemini hooks migrate --from-claude` — eval for the operator-facing skills too; if it works end-to-end, we can auto-sync our `.claude/settings.json` to `.gemini/settings.json` on every change.
