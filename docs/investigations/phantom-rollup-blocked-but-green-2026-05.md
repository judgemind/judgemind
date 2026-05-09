# Phantom rollup BLOCKED-but-green investigation

**Date:** 2026-05-08
**Issue:** #4068 (parent: #4058)
**Status:** Resolved — root cause was already fixed in PR #3200 (2025-12); the
"phantom" framing in #4058 is a misclassification of legitimate `StatusContext`
entries.

## TL;DR

- The "phantom rollup entry" described in #4058 / #4068 is **not a phantom**.
  It is a `StatusContext` entry (commit-status API, e.g. Vercel, codecov/patch)
  that has `state: "SUCCESS"` and no `conclusion` field. `StatusContext`s use
  `state`, not `conclusion` — that is the GitHub GraphQL schema.
- The `select(.conclusion == null)` query proposed as the AC2 diagnostic in
  #4058 surfaces every `StatusContext` entry, since they never have a
  `conclusion` field. This is what made the issue look like a recurring
  phantom problem.
- The canonical CI rollup classifier (`_ci_rollup_state` in
  `scripts/dispatcher/phase_transitions.py`, called from the daemon, the
  agent-runner ECS entrypoint, and `scripts/wait-for-ci.sh` via
  `scripts/dispatcher/ci_classifier_cli.py`) **already handles
  `StatusContext` entries correctly** — has done so since PR #3200 in
  2025-12. The classifier branches on `__typename`: a `StatusContext` with
  `state: SUCCESS` counts as green, not as pending or as a phantom.
- The "BLOCKED-but-green" failure mode is **not recurring in production
  today**: 0 occurrences in the last 14 days across both dispatcher daemon
  and agent-runner ECS log groups, against 283 merge attempts.

## Evidence

### 1. Probe across 16 recent merged PRs

`tmp/probe_phantoms.py` ran the AC2 diagnostic query (`conclusion == null OR
conclusion == "" OR name == null`) against the head SHAs of 16 recently
merged PRs (#4053 through #4488). Every single PR had at least one match,
but every match was a `StatusContext` with `state: "SUCCESS"` and a
non-null `targetUrl`:

```
PR #4053: phantoms=1 — typename=StatusContext context='Vercel' state='SUCCESS'
PR #4060–#4488: same shape (Vercel + occasional codecov/patch StatusContext)
```

Aggregation: `By __typename: {'StatusContext': 18}`,
`By context/name: {'Vercel': 16, 'codecov/patch': 2}`. **Zero `CheckRun`
entries with null/empty `conclusion`.** That is, no actual phantoms — every
"hit" the AC2 query produces is a properly-reported third-party status
context.

### 2. The classifier already handles StatusContext correctly

`scripts/dispatcher/phase_transitions.py:1308` (`_ci_rollup_state`) has been
the single source of truth across the daemon, agent-runner, and
`wait-for-ci.sh` since PR #4417. It explicitly branches on `__typename`:

```python
typename = str(check.get("__typename") or "").upper()
if typename == "STATUSCONTEXT":
    state = str(check.get("state") or "").upper()
    if state in _CI_STATUSCONTEXT_FAILURE_STATES:
        return "red"
    if state in _CI_STATUSCONTEXT_SUCCESS_STATES:  # SUCCESS / NEUTRAL
        continue
    # EXPECTED / PENDING / unknown / "" → not yet done.
    any_pending = True
    continue
```

The classifier docstring explicitly cites the original symptom:

> Before #3200 this function only inspected `.status` + `.conclusion` and
> fell through to "pending" for any StatusContext entry — so every PR that
> exposed a Vercel status stayed pending forever.

PR #3200 (commit `a702d657`, 2025-12-22) closed that exact bug class.

### 3. CloudWatch: BLOCKED-but-green is not happening in production

CloudWatch Logs Insights queries against `/ecs/judgemind-dispatcher-dev`
and `/ecs/judgemind-dispatcher-agent-runner-dev` for the window
2026-04-23 → 2026-05-08:

- `merge_stale_rollup_detected` events: **0**
- `merge_unstick_*` events: **0**
- `merge_done` events with `exit_code != 0`: **1 of 283** (PR #3582,
  2026-04-27 — a SIGPIPE crash with exit_code=141, NOT a stale-rollup
  rejection; the daemon's orphan-PR resurrection path subsequently
  merged the PR cleanly via `_merge_pr_and_advance`).
- All 282 other merges completed with `exit_code=0` and no fallback
  invocation.

The `STALE_ROLLUP_STDERR_MARKER = "base branch policy prohibits the
merge"` (constant defined in `scripts/dispatcher/daemon.py:1675` and
mirrored in `scripts/dispatcher/agent-runner-entrypoint.sh:4216`) is wired
to the auto-unstick path (push empty commit to force rollup re-evaluation),
but that path has not fired in the last 14 days.

### 4. Why #4053 looked like the canonical example

The worked example in #4058's body cites PR #4053:

```
$ gh pr merge 4053 --squash --delete-branch
X Pull request judgemind/judgemind#4053 is not mergeable: the base branch
  policy prohibits the merge.
```

PR #4053 was the docs PR that landed the A.7 SKILL.md fallback itself.
Its merge was performed manually by the operator, not by the dispatcher.
There is no corresponding `merge_begin` / `merge_done` / `merge_stale_rollup_detected`
event in the agent-runner ECS log group for PR #4053. The single observed
operator-side rejection that prompted #4058's authoring was a transient
GitHub-side state — the `mergeStateStatus` was `BLOCKED` momentarily
because GitHub had not yet finished re-evaluating the rollup after the
final `coverage-check` rerun completed. By the time the operator hit the
REST `PUT /merge` endpoint a few seconds later, the rollup had updated
and the merge succeeded.

This is consistent with PR #3200's docstring describing GitHub's PR
rollup cache as having ~10-minute lag relative to job-state changes —
the symptom was real but transient, and the operator-side workaround in
A.7 was reasonable belt-and-suspenders advice given the residual
uncertainty.

## Why the AC2 query produced false positives

Issue #4058 proposed this diagnostic:

```
gh api /repos/judgemind/judgemind/commits/<SHA>/check-runs \
  --jq '[.check_runs[] | {name, status, conclusion, app, started_at, completed_at}]'
```

This endpoint (`/commits/<SHA>/check-runs`) returns ONLY `CheckRun` entries
— it does NOT include `StatusContext` entries (those live under
`/commits/<SHA>/statuses`, the legacy commit-status API). So `gh api
.../check-runs` would have correctly reported "no phantom check_runs."

But the proposed `gh pr view --json statusCheckRollup --jq '.statusCheckRollup
| map(select(.conclusion == null or .conclusion == ""))'` query (the AC2
verify line) DOES surface `StatusContext` entries from the unified GraphQL
rollup. Those entries lack a `conclusion` field by GraphQL schema design,
so the filter selects them all — producing the appearance of a recurring
phantom.

The two queries operate on different data sources and answer different
questions:

- `gh api .../check-runs` — only GitHub Actions / GitHub Apps check_runs.
  The right place to look for a "registered but never reported" GitHub
  Apps integration.
- `gh pr view --json statusCheckRollup` — the heterogeneous union of
  CheckRun (GitHub Actions) AND StatusContext (third-party commit-status
  integrations like Vercel, codecov/patch). The right place to look for
  what GitHub uses to compute `mergeStateStatus`.

## Conclusion: no code change required for the phantom

There is no phantom check-runs entry in the rollup of any recent PR. The
appearance of one in the AC2 diagnostic is an artifact of the query treating
`StatusContext` entries as if they were CheckRuns. The classifier code path
that actually decides "green vs pending" already handles `StatusContext`
correctly via `__typename` branching, since #3200.

## Residual: the SKILL.md A.7 fallback

`.claude/skills/task/SKILL.md` §A.7 still documents a "phantom rollup entry"
fallback that uses `gh api /repos/.../pulls/<N>/merge -X PUT` when
`gh pr merge` returns "base branch policy prohibits the merge." The fallback
itself is a legitimate workaround for a different (real) failure mode:

- `gh pr merge`'s pre-flight is stricter than the REST `PUT /merge`
  endpoint: it refuses on any `mergeStateStatus=BLOCKED`, including the
  transient case where GitHub's rollup has not yet finished recomputing
  after a CI rerun.
- The REST `PUT /merge` endpoint re-evaluates branch protection at the
  moment of the call and accepts the merge if the latest required check
  conclusions are SUCCESS, regardless of the cached `mergeStateStatus`.

So the fallback is correct — but its **explanation** is misleading. The
A.7 prose attributes the BLOCKED state to "a phantom rollup entry: a check
that registered on the SHA but never reported a final conclusion." That
description does not match observed reality (no such phantom exists in
recent rollups), and reframes the legitimate transient-rollup-lag failure
mode as something it is not.

The corrective action shipped with this investigation is to update SKILL.md
A.7 to:

1. Drop the "phantom check that registered but never reported" framing.
2. Replace it with the actual root cause — `gh pr merge`'s strict
   pre-flight on `mergeStateStatus=BLOCKED` versus the REST endpoint's
   willingness to accept the merge when the latest required checks are
   green.
3. Note that the fallback is rarely needed in production (zero firings in
   the dispatcher logs over the last 14 days).

## Follow-up issues filed

None. This investigation closes #4068 and #4058 (the parent). The SKILL.md
A.7 update is shipped in the same PR as this investigation doc.

## Related

- #4058 — the original issue that documented the operator-side fallback
- PR #4059 — the PR that landed the (mis-described) fallback in SKILL.md
- PR #4053 — the worked-example PR cited in the SKILL.md doc
- PR #3200 — the dispatcher fix that wired StatusContext into the
  canonical classifier (2025-12)
- PR #4417 — the refactor that consolidated the classifier across the
  daemon, agent-runner, and wait-for-ci.sh
- #2641 — the dispatcher daemon's separate empty-commit auto-unstick path
  (different layer; addresses the same transient-rollup-lag failure mode
  by forcing a rollup recompute on a fresh SHA)
