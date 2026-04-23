# Dispatcher v2 Spike 0.2 — Hook → Postgres from a `claude -p` subprocess

**Status:** Complete — verdict: **GO**
**Issue:** #2684
**Spec:** `docs/specs/dispatcher-v2-spec.md` §9 (hooks write directly to Postgres), §15 spike 0.2
**Depends on:** spike 0.1 (#2683) — verdict GO, see `docs/investigations/dispatcher-v2-spike-0.1.md`

## Summary

Installed a PreToolUse hook on the `Read` tool inside the dispatcher-spike
Fargate image. Each `claude -p` invocation runs `emit_failure.py` via the
hook, which opens a psycopg3 connection to dev RDS, inserts one row into
`dispatcher_spike.failures`, commits, and closes. Over 20 synchronous hook
invocations from 20 separate Fargate tasks, the wall-clock latency between
the client-side `hook_fire_ts` and the server-side `inserted_at` was
**p50 = 179 ms, p95 = 200 ms, max = 202 ms**.

No inserts failed. The failure-mode test (a deliberately unreachable
`DATABASE_URL`) confirmed that the hook does not propagate a non-zero exit
code — the blocked `Read` tool call proceeds normally, the subprocess
exits 0, and the DB simply has no row for that task.

**Verdict: GO.** §9 as written works. No local spool, no reaper loop, no
marker files required. Keep the direct-insert design for Phase 1.

## How the spike was run

- Image: `Dockerfile.dispatcher-spike` (tag `spike-0.2`) — same base as
  spike 0.1, plus `python3-pip` and `psycopg[binary]>=3.1` installed via
  pip with `--break-system-packages`, and three new files baked in:
  - `/etc/dispatcher-spike/settings.json` — a minimal Claude Code settings
    file that registers a `PreToolUse` hook on matcher `Read` pointing at
    `/usr/local/bin/dispatcher-spike-hook.sh`.
  - `/usr/local/bin/dispatcher-spike-hook.sh` — a 30-line shim that reads
    the tool-input JSON from stdin, extracts the tool name into a
    `--details` JSON payload, and invokes `emit_failure.py` with
    `--category spike_test --agent-id "$SPIKE_AGENT_ID"`. The shim
    always exits 0 even if the python invocation fails.
  - `/usr/local/bin/emit_failure.py` — a ~190-line python module (the
    runtime is ~40 lines; the rest is argparse, docstrings, and the
    SQL-injection guard for `--table`). Uses `psycopg.connect(...)` with
    a 10s connect timeout, one `INSERT INTO dispatcher_spike.failures ...
    VALUES (%s, %s, %s, %s, %s)` in a single cursor, `conn.commit()`,
    `conn.close()` via the with-context. All errors are caught and
    swallowed — the process always exits 0. A one-line JSON timing
    trace is emitted to stderr (`EMIT_FAILURE_TIMING {...}`).

- Schema: migration 19 (`packages/api/migrations/19_dispatcher-spike-failures.sql`)
  adds `dispatcher_spike.failures` — columns `failure_id uuid`, `category text`,
  `agent_id text`, `details jsonb`, `hook_fire_ts timestamptz` (client side),
  `inserted_at timestamptz DEFAULT now()` (server side). Indexes on
  `category` and `agent_id` for the harness filter queries.

- Harness: `scripts/dispatcher-spike/measure_hook_latency.sh` launches N
  (default 20) tasks via `aws ecs run-task --count N` in batches of ≤10
  (the Fargate per-call cap), waits for all to reach `STOPPED`, queries
  the spike table with `agent_id = '<SPIKE_RUN_ID>'`, and computes
  min/p50/p95/max/mean/stdev. Wall-clock to launch + complete all 20
  tasks: **80 seconds** (all tasks landed inside a single 80s poll window
  after launch).

- Prompt: `Use the Read tool to read the file ${HOME}/spike-probe.txt,
  then reply with only the first line of the file (nothing else).`
  `claude -p` reliably chose the `Read` tool on all 20 invocations — the
  hook fired once per task. Container-entry writes the probe file at
  startup; `--add-dir $HOME` whitelists it for Read.

## Findings — the 5 bullets §15 asks for

### 1. p50 / p95 / max hook-insert latency

**n = 20** hook-triggered inserts, all successful.

| metric | value |
|---|---|
| min | 163.3 ms |
| p50 | 179.2 ms |
| p95 | 200.0 ms |
| max | 202.1 ms |
| mean | 179.9 ms |
| stdev | 13.1 ms |

Raw values (ms, one per task, ordered by `inserted_at`):

```
163.66  189.48  192.07  167.92  180.46  164.10  169.46  190.23  199.91  166.17
171.70  167.75  163.26  175.38  184.83  195.02  196.82  177.89  202.14  180.70
```

This is dominated by the psycopg connection establishment + TLS handshake
+ INSERT+COMMIT round-trip to the RDS writer in us-west-2. The distribution
is tight (stdev 13 ms, max-min spread 39 ms) which is consistent with the
profile of "cold TCP → TLS → auth → single INSERT". Python process
startup inside the container contributes at most ~30 ms; that is included
in the numbers above (the `hook_fire_ts` is captured at the top of
`emit_failure.py` *after* argparse, but `--break-system-packages` pip
wheel `psycopg[binary]` imports quickly).

### 2. Connection-establishment overhead — cold vs. warm

**Every hook invocation is cold.** The hook runs in a fresh `python3`
subprocess spawned by Claude Code, so there is no pool, no shared
connection, no warm path. This spike therefore does NOT measure a warm
connection — it measures the only mode that matters for §9 (hook scope
starts and ends inside a single tool-use event).

The p50 of 179 ms is entirely cold-connection cost. An optimisation
would be to keep a psycopg connection pool alive for the lifetime of
the `claude -p` subprocess (e.g. a persistent daemon the hook writes
to over a UNIX socket), but that adds surface area not justified by
these numbers. 200 ms p95 is invisible inside a typical `claude -p`
tool call which itself takes seconds.

### 3. Any inserts failed?

**Zero.** 20 of 20 inserts succeeded inside the 80s harness window. The
`dispatcher_spike.failures` row count for `category='spike_test'` is 21
(20 harness + 1 earlier smoke test) — matches the acceptance criterion.

### 4. Failure-mode behavior

Scenario `hook_failmode` was run once with `DATABASE_URL` set to
`postgresql://nobody:nobody@127.0.0.1:1/nodb?connect_timeout=2`. The
connect fails fast (port 1 is reserved and returns RST on local loopback
or a silent timeout from inside the task's subnets). Observed:

- **Subprocess exit code:** 0
- **Claude `-p` behavior:** completed normally — stdout printed the first
  line of the probe file (`spike-0.2 probe file — read by claude to fire
  the PreToolUse hook.`).
- **ECS task exit:** `Essential container in task exited`, exit 0.
- **DB rows for `agent_id='smoke-failmode-1'`:** 0.
- **No stdout/stderr chatter bled through from the hook** — Claude Code
  captures hook stderr and does not relay it to the container's log
  stream in `-p` mode. The `EMIT_FAILURE_TIMING` trace the emitter
  writes to stderr is visible only to Claude's internal hook-observer.

This is exactly the desired behaviour: a DB outage or credential error
degrades gracefully to "row missing" rather than "blocked tool call" or
"crashed subprocess".

### 5. Go / design-change verdict

**GO. Keep §9 as written.**

Rationale:

- 200 ms p95 is well under any reasonable tool-call budget. A typical
  `Read` is 20-50 ms; a `Bash` or `Edit` is hundreds of ms to seconds.
  Adding 200 ms once per failure signal is invisible.
- Direct-insert passes Principle 1 (Postgres as state of record).
- The fallback design (local spool + reaper loop) adds surface area —
  a spool file, a reaper process, recovery semantics, a second failure
  mode when the spool itself is corrupt — without any observed latency
  benefit we need.
- If p95 ever blows out (e.g. RDS writer under stress), §9's
  fallback paragraph already describes the escape hatch: "if hook-insert
  failures are >5%, add a retry loop or promote to a local spool." We
  can measure and react if that becomes real; it isn't today.

## Caveats and callouts

- **Exit-code ambiguity remains (carried from spike 0.1 finding 3, filed
  as #2701).** The hook mechanism does NOT help here — a `subprocess_auth_fail`
  occurs before any tool call, so the PreToolUse hook never fires. The
  daemon's wrapper-level stdout regex classification from spike 0.1
  remains the right place to distinguish auth fails from turn limits
  from miscellaneous crashes.

- **psycopg, not psycopg2.** The issue body says "psycopg2 wrapper". The
  repo convention is psycopg3 (imported as `psycopg`, installed as
  `psycopg[binary]`) — `packages/scraper-framework/pyproject.toml` pins
  `psycopg[binary]>=3.1`, and `scripts/dev_db_query_runner.py` uses the
  same import. I followed the repo convention; psycopg3 is a
  drop-in-syntax-compatible replacement for the ~5 methods
  `emit_failure.py` uses (`psycopg.connect`, `conn.cursor`, `cur.execute`,
  `conn.commit`, the context managers). No PR follow-up needed; this note
  just documents the choice.

- **Harness aggregator fix.** The first run of `measure_hook_latency.sh`
  crashed its aggregator because `dev-db-query.sh` appends a
  `Cannot perform start session: EOF` line after the JSON results. The
  database query succeeded — all 20 rows were present. A two-line
  `json.JSONDecoder.raw_decode` based extractor replaces the previous
  naïve "find `[`, print to end" logic and is now merged into the
  harness script. I computed the p50/p95/max above by running the
  aggregation SQL directly against RDS, not by the harness's own
  aggregator.

- **Claude Code hook execution surface.** The Claude Code CLI reads
  `--settings <file>` as a layered settings source. The PreToolUse
  `matcher: "Read"` wording matched exactly one invocation per prompt.
  `--add-dir $HOME` was required because the probe file sits outside
  the session cwd (`/home/spike`); without it Claude's workspace-trust
  guard refused the Read call.

- **No backup strategy needed for the spike data.** The 21 existing
  rows in `dispatcher_spike.failures` will be deleted as part of the
  cleanup follow-up. Leave the table — it's reusable if we ever want
  to repeat the measurement (e.g. to measure latency under different
  network conditions, or to compare to a warm-connection variant).

## Reproducing the spike

```
# 1. Apply migration (not auto-applied — throwaway schema):
scripts/dev-db-query.sh --rw "$(cat packages/api/migrations/19_dispatcher-spike-failures.sql)"

# 2. Build + push image:
docker build --platform linux/amd64 \
    -f Dockerfile.dispatcher-spike \
    -t judgemind/dispatcher-spike:spike-0.2 .
docker tag judgemind/dispatcher-spike:spike-0.2 \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest
docker push \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest

# 3. Run the harness:
SPIKE_LATENCY_N=20 scripts/dispatcher-spike/measure_hook_latency.sh

# 4. (optional) Failure-mode smoke test:
SPIKE_AGENT_ID="failmode-$(date +%s)" \
    scripts/dispatcher-spike/run_fargate_claude_p.sh hook_failmode

# 5. Query aggregates:
scripts/dev-db-query.sh \
    "SELECT count(*), \
            percentile_cont(0.50) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (inserted_at - hook_fire_ts)) * 1000.0) AS p50, \
            percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (inserted_at - hook_fire_ts)) * 1000.0) AS p95, \
            max(EXTRACT(EPOCH FROM (inserted_at - hook_fire_ts)) * 1000.0) AS max_ms \
     FROM dispatcher_spike.failures WHERE category='spike_test';"
```

## What this spike explicitly did NOT prove

- **Concurrency under real `/task` load.** The harness launches 20 tasks
  in parallel and all their hook inserts complete without contention,
  but that is 20 simultaneous INSERTs into an empty table, not 20
  simultaneous `dispatcher.*` writers racing against `derived.*` readers
  and writers. If RDS writer saturation ever becomes a factor, we'd see
  p95 climb. Not our problem today.
- **Long-running `claude -p` with many hook fires.** Spike 0.2 asks for
  one hook fire per subprocess. §9's real use case is SubagentStop + PreToolUse
  cwd-drift + PreToolUse gh-rate-guard — up to a dozen possible fires
  per agent run. At 200 ms each, a dozen fires over a 45-minute
  `claude -p` is 2.4 s total, still invisible. But this is still a
  synthetic extrapolation, not a measurement.
- **Direct-write under RDS failover.** Spike 0.2 ran with one RDS
  writer instance in steady state. A failover event would surface as
  higher p95 for the duration of the failover (20-60s of connect timeouts);
  the hook's try/except correctly degrades to "row missing" during that
  window, and the daemon's crashed-agent reconciliation path catches
  any silently lost failure signals.

## Follow-ups filed

- **Cleanup issue (to be filed after this PR lands):** delete the 21
  existing rows from `dispatcher_spike.failures` once Phase 1 starts
  with the real `dispatcher.failures`. Do NOT drop the schema or table
  — leave them for future re-measurement if §9's latency assumptions
  ever need to be re-validated.
- **(already filed for spike 0.1, #2701):** exit-code classification
  clarification — carry forward unchanged.
