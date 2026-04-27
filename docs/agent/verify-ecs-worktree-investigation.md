# Verify ECS Worktree Investigation — Issue #3547

## Summary

Agent `6807ed3a` shipped PR #3546 cleanly (merged + deployed) but the verify phase
returned `verdict=FAILED, failure_reason="input JSON missing or malformed"`.
`/diagnose-failure` annotated the reason with `— worktree path does not exist` after
checking that the daemon DB row's `worktree_path` (`/var/lib/dispatcher/worktrees/agent-6807ed3a`)
does not exist on the dispatcher host.

---

## Which process launched verify?

**The ECS agent-runner-entrypoint.sh running inside the Fargate task.**

The daemon correctly skipped verify for this agent via two independent guards:

- `daemon.py:13904` — `_advance_running_agents` short-circuits every phase for
  `execution_mode='ecs'` rows (event: `supervise_skipped_ecs_agent` or
  `verify_skipped_ecs_agent`). This was installed by #3196.
- `daemon.py:15327` — `_advance_awaiting_deploy` has a belt-and-suspenders
  ECS guard for direct-call / recovery paths.

Neither guard was violated. The agent-runner's own state machine in
`agent-runner-entrypoint.sh` advanced from `awaiting_deploy` → `verify` at
line 5110 (`advance_phase "verify"`) and then entered the `planning|ralph|summary|verify`
case at line 4639, which calls `run_claude_phase "verify"`.

---

## What `worktree_path` did the verify SKILL receive?

**It never received one.** The input file was never written.

The agent-runner's `run_claude_phase` calls `write_phase_input "$_skill" || true`
(line ~1851). The `|| true` is intentional best-effort — if the shim fails, execution
continues and `claude -p /task-v2-verify` is invoked without a
`tmp/dispatcher-input/verify.json` file. The SKILL then hits its guard:

```
If the file is missing or malformed, exit 0 with
verdict=`FAILED, failure_reason="input JSON missing or malformed"`.
```

The diagnoser's `— worktree path does not exist` annotation is its own post-hoc
observation of the DB row's `worktree_path` field (`/var/lib/dispatcher/worktrees/agent-6807ed3a`,
set when the daemon created the worktree before launching the ECS task). This path
exists on the dispatcher host at launch time but not at verify time (worktree cleanup
had already run). The annotation was the diagnoser inferring _why_ the input might be
missing — but the actual cause is one level deeper in the shim.

---

## Why did the shim fail to write the input file?

**Root cause: `_run()` calls in `_fetch_merged_pr_info`, `_fetch_deploy_runs_for_sha`,
and `_fetch_issue_bundle` lacked `try/except`. A `subprocess.TimeoutExpired` (or
other subprocess exception) from any `gh` CLI call propagated uncaught to `main()`,
exiting the shim with code 1.**

### Code path

`_build_verify_input()` calls three `gh` helpers:

```python
# agent-runner-entrypoint.sh (embedded shim, ~line 1343-1378)
def _build_verify_input(...):
    pr_number = _db_fetch_agent_pr_number(agent_id)   # has try/except
    merge_info = _fetch_merged_pr_info(github_repo, pr_number)
    # ...
    deploy_runs = _fetch_deploy_runs_for_sha(github_repo, merge_sha)
    # ...
    bundle = _fetch_issue_bundle(github_repo, issue_number)
```

`_fetch_merged_pr_info` (before the fix):

```python
def _fetch_merged_pr_info(repo, pr_number):
    cmd = ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "..."]
    outcome = _run(cmd, timeout=30)   # NO try/except — TimeoutExpired propagates!
    if outcome.returncode != 0:
        return {"merge_commit_sha": "", "pr_state": ""}
```

`_run()` uses `subprocess.run(..., timeout=30)`. If the `gh` subprocess runs longer
than 30 seconds (GitHub API congestion, network hiccup, token rate-limit response
delay), `subprocess.run` raises `subprocess.TimeoutExpired`. This is NOT a subclass
of `subprocess.SubprocessError` that the caller handles with `if returncode != 0` —
it's an exception, and it propagates up through `_build_verify_input` → `_build_input`
→ `main()`, causing Python to exit non-zero.

The shell caller:

```bash
write_phase_input "$_skill" || true   # line ~1851 in agent-runner-entrypoint.sh
```

The `|| true` swallows the non-zero exit from the shim. `claude -p /task-v2-verify`
then runs in a directory where `tmp/dispatcher-input/verify.json` was never created.

### Confirmation: the shim DOES have try/except in some helpers but NOT others

Functions that ARE correctly guarded:
- `_db_query_one` — `except Exception: return ""`
- `_fetch_job_log_tail` — `except Exception: return ""`
- `_git_diff`, `_git_changed_files`, `_git_current_branch`, `_git_diff_stats` — all wrapped

Functions that were MISSING `try/except` before the fix:
- `_fetch_issue_bundle` — calls `_run(cmd, timeout=30)` bare
- `_fetch_pr_status` — calls `_run(cmd, timeout=30)` bare
- `_fetch_pr_diff` — calls `_run(cmd, timeout=60)` bare
- `_fetch_merged_pr_info` — calls `_run(cmd, timeout=30)` bare
- `_fetch_deploy_runs_for_sha` — calls `_run(cmd, timeout=30)` bare

### DB query results

DB queries via `scripts/dev-db-query.sh` were unavailable from this environment
(SessionManagerPlugin not installed), so the above chain was established through
static code analysis of `agent-runner-entrypoint.sh`.

---

## Fix applied (Outcome A)

1. **Added `try/except Exception` around `_run()` calls** in all five gh helper
   functions in the embedded phase-input shim inside `agent-runner-entrypoint.sh`.
   Each `except` branch returns the same safe empty value (empty dict / empty list /
   empty string) that the existing `if returncode != 0` branch returns — so the shim
   always writes a partial-but-valid input JSON even when `gh` times out.

2. **Added top-level `try/except` in `main()`** to catch any unforeseen exception
   from `_build_input` (new code path, import error, disk full on write). Exits with
   code 2 and prints a descriptive stderr message so CloudWatch operators see the
   specific cause rather than "input JSON missing or malformed" from the SKILL.

3. **Improved `write_phase_input` logging**: now captures and logs the shim's stderr
   tail on failure (`shim_err_tail=`) so the `phase_input_write_failed` CloudWatch
   event carries the actual exception text rather than being an opaque non-zero exit.

4. **Added regression test (Test 21b)** in `scripts/tests/test_agent_runner_entrypoint.sh`:
   runs the verify shim against a gh stub that always exits 1 and asserts that (a)
   the shim exits 0, (b) `dispatcher-input/verify.json` is written, (c) the file
   carries `agent_id` and a `worktree_path` matching the Fargate `REPO_ROOT`
   (not a subprocess-lane path from the dispatcher DB row).

---

## AC3 telemetry query (post-deploy, deferred)

After this fix deploys, run the following against dev to confirm the failure rate drops:

```sql
SELECT date_trunc('day', ts) AS day, count(*) AS failures
FROM dispatcher.failures
WHERE category = 'verify_failed_post_merge'
  AND details->>'failure_reason' LIKE '%input JSON missing or malformed%'
  AND ts > now() - interval '14 days'
GROUP BY 1
ORDER BY 1;
```

A healthy post-deploy trend shows 0 rows with `ts` after the deploy date.
Rows before the deploy date represent the historical occurrence of this bug.

---

## Follow-up issues to file

- If future investigation finds the agent-runner's `write_phase_input` shim is broken
  for other phases (not just verify), file a class-wide audit issue to add a startup
  self-check that exercises the shim for every phase before the agent loop begins.
- Issue #3338 covers the broader `verify_failed_post_merge → route_stub` class.
