# `profile-shell-test.sh` rc=2 flake on green wrapped test — 2026-05

**Issue:** #4383
**PR:** TBD (this PR)
**Worktree:** `agent-ab5189aec7f301993`
**Date:** 2026-05-08

## TL;DR

`scripts/tests/test_profile_shell_test.sh` Test 13 (the AC integration check)
intermittently fails with:

```
PASS: entrypoint test profiled to ≥ 30 sections (got 55, floor 30)
PASS: entrypoint test PASS count ≥ 468 (issue baseline) (got 474, floor 468)
PASS: entrypoint test runs all 474 sub-tests
FAIL: profiler exit matches wrapped test (all green → rc=0)
  expected: 0
  actual:   2
```

The wrapped entrypoint test passes 474/474 internally, yet the profiler
returns rc=2.

The root cause is a structural fragility in the EXIT-trap chain
`scripts/profile-shell-test.sh` injects into the wrapped script. When the
wrapped script terminates with `set -e` in force, the chained EXIT trap
(`_section_close; cleanup`) runs under `set -e`. Any non-zero rc from a
command inside `_section_close` (e.g. a flaky `python3 -c '…'` call inside
`_section_now_ms`, a `sed` race, an arithmetic-failure on a transient
empty state file) propagates as the script's exit code — overriding the
wrapped test's `exit 0`. This makes the profiler report "wrapped test
exited rc=2" when in fact the trap itself died with rc=2.

`scripts/tests/test_agent_runner_entrypoint.sh` is the wrapped test that
exposes this: line 32 sets `set -uo pipefail` (no `-e`), but each of its
sub-tests toggles `set +e` / `set -e` around `bash -c '…'` captures. The
final toggle is at line 10275 (`set -e`), so when the script reaches
`exit 0` at line 10295, **`set -e` is in force** — and the profiler's
injected trap inherits it.

## Evidence chain

### 1. The flake is real and reproducible from the CI log

Same SHA `4c2e44c2`, two CI runs:

* push-trigger run [25581648818](https://github.com/judgemind/judgemind/actions/runs/25581648818) — PASSED.
* PR-create-trigger run [25581668280](https://github.com/judgemind/judgemind/actions/runs/25581668280) — FAILED on the rc=2 assertion (job
  `scripts-tests (shell-slow, 6, slow)` log lines 870-872).

Re-running the failed jobs produced an immediate green. Local 10×
repro on macOS (this worktree) showed 0/10 failures — flake density
is low and the failing run does not reproduce on a quiet workstation,
matching the "Instrument before you guess" pattern in the issue.

### 2. Where rc=2 can come from (catalog)

Exhaustively, the rc=2 originates from one of:

1. **The wrapped test itself called `exit 2`.** Searched
   `scripts/tests/test_agent_runner_entrypoint.sh` for `exit 2`: the
   only matches are at lines 5017 and 5069, both inside `bash -c '…'`
   sub-shells whose rc is captured into `t50_rc` / `t50b_rc` and
   asserted on locally. They do not propagate to the wrapped test's
   final exit. Falsified.

2. **`scripts/profile-shell-test.sh` itself hit one of its own `exit 2`
   paths** (missing input file, arg-parse error). Falsified — the input
   exists and the args parse cleanly; the test passes 25/26 with the
   profiler running to completion (recorded 55 sections).

3. **The chained EXIT trap injected by the profiler died with rc=2
   while `set -e` was in force at the wrapped test's exit.** This is
   the surviving hypothesis.

### 3. The wrapped test exits with `set -e` in force

`grep -nE "(^|\s)set [+-]e" scripts/tests/test_agent_runner_entrypoint.sh`
shows the toggle pattern. The final two `set` directives, sorted by
line number, are:

```
10254:set +e
10275:set -e
```

Then line 10287-10295 (the final summary block, no further `set` calls)
ends with `exit 0`. So when the EXIT trap fires, `set -e` is in force.

### 4. Under `set -e`, an EXIT trap that returns rc=N exits the shell with rc=N

Verified locally with a one-shot reproducer at
`tmp/trap_test7.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
section_close() {
    python3 -c 'import sys; sys.exit(2)' 2>/dev/null
    echo "should not reach"
}
trap 'section_close' EXIT
exit 0
```

Run output: `rc=2`. The script's `exit 0` is overridden by `set -e`
firing on the python rc=2 inside the trap. The same shell semantics
apply when `set -e` is in force at the moment EXIT fires, regardless of
where it was last toggled.

### 5. The profiler's `_section_close` is a thin wrapper around `_section_now_ms`, which calls `python3 -c '…'`

`scripts/profile-shell-test.sh:215-222`:

```bash
_section_now_ms() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time; print(int(time.time()*1000))'
    else
        printf '%s000\n' "$(date +%s)"
    fi
}
```

`_section_close` (`scripts/profile-shell-test.sh:251-271`) opens with
`local _now=$(_section_now_ms)`. Under `set -e`, a non-zero rc from this
command-substitution can propagate (the bash 5+ default with the script
running on the CI Linux runner; the shopt `inherit_errexit` defaults
also matter — see §6 below).

`python3 -c 'import time; …'` is normally rock-solid, but rc=2 from
python is exactly what `argparse` and `SystemExit(2)` produce — including
when the python interpreter is interrupted by an EINTR-class signal at
just the wrong moment, or when a `sigchld` from a sibling process trips
the interpreter's signal handlers during startup.

Other rc=2 candidates inside `_section_close`:

* `sed -n '1p' "$_PROFILE_STATE"` — if the state file is gone (it's a
  regular file under the profiler's `mktemp -d` workdir, but a parallel
  shard race could plausibly delete it). sed exits 2 on missing file.
* `printf '%d.%s\t%s\n' … >> "$_PROFILE_TSV"` — if the TSV file is
  gone for any reason.

Each candidate is rare in isolation; together they form a small but
non-zero probability tail under CI runner load.

### 6. Why the local 10× reproducer doesn't fire

The flake density observed in CI is roughly 1-in-tens-of-runs (one
observed failure across the SHA's two CI attempts plus an unknown
number of priors). On a quiet macOS box, with no sibling processes
generating signal traffic, the `_section_now_ms` python invocation
completes deterministically.

CI runners are different: they run the full test under a SHARD_FILTER
where parallel shards are not guaranteed to stop touching the runner's
process table. Signal-driven flakes are exactly the kind of "1-in-N
runs" failure that doesn't reproduce on a developer laptop. This
matches the "Instrument before you guess" pattern from
`docs/agent/investigation-patterns.md` — the right move is to
make the next failure self-diagnosing, not to patch the top hypothesis.

### 7. Why this is the surviving hypothesis (counter-evidence)

* **It explains the rc=2 specifically.** rc=1 (false), rc=127 (cmd not
  found), rc=130 (SIGINT), rc=137 (SIGKILL) would all be plausible
  failures from a flaky trap; rc=2 is a much narrower fingerprint that
  matches python's `SystemExit(2)` / `argparse` / `sed` missing-file
  rc-2 specifically.
* **It explains why "all 474 internal tests passed but profiler exited
  rc=2."** The internal tests really did all pass; the wrapped script
  reached `exit 0`; the trap is the only thing that runs after that.
* **It explains why a re-run is green.** Trap-side flakes don't carry
  over between CI runs.
* **It explains why local repro fails.** Different signal regime.

## Fix

Two structural fixes plus instrumentation, shipped together:

1. **Harden `_section_close` and `_section_record` against any rc leak.**
   The injected functions must never propagate a non-zero rc into the
   wrapped script's exit code, regardless of whether `set -e` is in
   force at the time the trap fires.
   * Wrap the whole body in a `set +e` / restore-prior-flags pattern
     (bash 3.2 compatible).
   * `|| true` every operation that touches the filesystem or shells
     out (`sed`, `printf >>`, `: >`, `_section_now_ms`).
   * Always `return 0` at the end.

2. **Stop discarding stderr in the AC test.** Capture profiler stderr
   to a file; on assertion failure, dump it. This makes the next
   failure self-diagnosing without re-rolling the dice.

3. **Add a one-line structured stderr beacon to
   `scripts/profile-shell-test.sh`** that records, at the moment of
   exit, the wrapped test's actual exit code separately from the
   profiler's own. Format:
   `profile-shell-test: wrapped_exit=N sections=N tsv=PATH`. This
   distinguishes "wrapped test exited rc=N" from "profiler died with
   rc=N" for any future flake.

A regression test at the layer of the bug — a shell-only fixture that
installs `set -e` + `trap user_cleanup EXIT` + sleeps + `exit 0`,
then asserts the profiler's rc=0 even when an injected
`_section_now_ms` shim deliberately exits 2 — would be the ideal proof.
The fix's own correctness can be proven structurally (the function
unconditionally returns 0 because every interior command is `|| true`'d
and `set +e` is in force inside the body), so the regression test is
implemented as: "set up a wrapped script that returns rc=0 with an
EXIT trap that we can deliberately make flaky, run it through the
profiler, assert profiler rc=0."

## Related

* #4188 — Widen sleep gaps in basic-fixture test to defeat CI jitter
  (similar shape; different root cause).
* #4191 — jitter follow-up.
* #4183 — pattern widening.
* #4181 — initial profiler.
* #4176 — AC integration: ≥30 sections from entrypoint test.
* `docs/agent/investigation-patterns.md` §"Instrument before you guess"
  — pattern this PR exemplifies.

## Stale-docstring updates (B.1.5)

None. The `profile-shell-test.sh` docstring covers behavior that the
fix preserves — it does not claim "_section_close cannot fail." The
trap-rewriting block's comments at lines 297-301 do say "the only data
loss is when a test exits inside its very last section AND overwrites
our trap" — that statement is still true; this fix is orthogonal
(it's about the trap chain not leaking rc, not about which trap wins).
