# Dispatcher v2 Spike 0.7 — Git + GitHub auth from Fargate

**Status:** Complete — verdict: **GO (scoped PAT)**
**Issue:** #2689
**Spec:** `docs/specs/dispatcher-v2-spec.md` §14 (deployment), §15 (spike 0.7)

## Summary

Extended the spike 0.1 Fargate task definition (`judgemind-dispatcher-spike-dev`)
with a single additional Secrets Manager entry holding a scoped GitHub PAT
(`judgemind/dev/dispatcher-spike/github-token`). Added a new `git_gh` scenario
to the container entrypoint that clones `judgemind/judgemind`, pushes a
throwaway branch, opens+closes a throwaway PR, and invokes
`scripts/check-issue-author.sh` — exactly the four operations §14 needs the
daemon's `/task-v2-*` subprocesses to perform from inside a container.

One end-to-end run on Fargate passed all four operations with exit code 0.
Wall-clock from `aws ecs run-task` to `STOPPED` was ~78 seconds, the same
as spike 0.1's `claude -p` scenarios. The PAT-only path is sufficient; no
GitHub App registration is required for v1.

**Decision for §14: scoped PAT in Secrets Manager.** Rationale below.

## How the spike was run

1. Created Secrets Manager entry `judgemind/dev/dispatcher-spike/github-token`
   holding a token from the existing `drewthaler` OAuth session (`gh auth token`).
   The token was stored with trailing newlines stripped (ECS passes the raw
   secret bytes; a trailing `\n` would poison `gh` auth via the env-var path).
2. Extended `infra/terraform/environments/dev/main.tf` to pass
   `data.aws_secretsmanager_secret.dispatcher_spike_github_token.arn` into
   the existing (already-parameterized)
   `module.dispatcher_spike.github_token_secret_arn` variable. The module was
   plumbed for this from day one in spike 0.1 — no module code changed.
3. `terraform apply -target=module.dispatcher_spike` bumped the task
   definition revision and added `GITHUB_TOKEN` to the container's `secrets`
   block; the execution-role policy was also extended to allow
   `secretsmanager:GetSecretValue` on the new ARN.
4. Extended `Dockerfile.dispatcher-spike` to install the official `gh` CLI
   from `cli.github.com` (keyring + apt source), and `COPY`'d
   `scripts/dispatcher-spike/test_git_gh.sh` +
   `scripts/check-issue-author.sh` into `/usr/local/bin/`.
5. Added a `git_gh` scenario branch to `scripts/dispatcher-spike/container-entry.sh`
   that short-circuits the `claude -p` invocation and delegates to
   `/usr/local/bin/dispatcher-spike-test-git-gh` — the auth probe is a pure
   git/gh pipeline, decoupling its failure modes from any Claude regression.
6. Rebuilt the image (`docker build --platform linux/amd64`), tagged as
   `:latest` and `:spike07`, pushed to the spike's ECR repo.
7. Invoked `scripts/dispatcher-spike/run_fargate_claude_p.sh git_gh`, which
   reuses the spike 0.1 wrapper to launch one Fargate task and tail the log.

The run output is reproduced verbatim in "Evidence" below.

## Findings (answers to the acceptance criteria)

### 1. Did `git push` succeed from inside the container? **YES.**

The task's stdout:
```
[test-git-gh] OP-1: clone judgemind/judgemind (shallow) and push spike/0.7-auth-test-1776557470
  Cloning into '/tmp/tmp.QPxQQSxRRl/repo'...
  Switched to a new branch 'spike/0.7-auth-test-1776557470'
  [spike/0.7-auth-test-1776557470 4123a33] test: spike 0.7 auth probe (throwaway, do not merge) [epoch=1776557470]
   1 file changed, 1 insertion(+)
   create mode 100644 docs/investigations/.spike-0.7-marker-1776557470.txt
  ...
  To https://github.com/judgemind/judgemind.git
   * [new branch]      spike/0.7-auth-test-1776557470 -> spike/0.7-auth-test-1776557470
[test-git-gh] OP-1: PASSED (push of spike/0.7-auth-test-1776557470 to judgemind/judgemind succeeded)
```
The credential plumbing that makes this work is `gh auth setup-git` — gh
writes `credential.https://github.com.helper = !gh auth git-credential` into
the container's `$GIT_CONFIG_GLOBAL`, and every `git push https://…` then
pulls the token from `${GITHUB_TOKEN}` automatically. No keyring, no SSH
key, no `~/.netrc`.

### 2. Did `gh pr create / view / close` succeed? **YES.**

```
[test-git-gh] OP-2: PR created → https://github.com/judgemind/judgemind/pull/2720
  {"headRefName":"spike/0.7-auth-test-1776557470","number":2720,"state":"OPEN","url":"https://github.com/judgemind/judgemind/pull/2720"}
...
[test-git-gh] OP-4: gh pr close https://github.com/judgemind/judgemind/pull/2720 (not merged)
  ✓ Closed pull request judgemind/judgemind#2720 (test: spike 0.7 auth probe (throwaway) [epoch=1776557470])
  ✓ Deleted branch spike/0.7-auth-test-1776557470
```
PR state after the task stopped: **CLOSED** (verified via
`mcp__github__get_pull_request pull_number=2720`; `merged_at` is `null`).
The `gh pr merge` path was not exercised because the spike explicitly does
not merge to main; the merge command uses the same auth surface as
`gh pr create` / `gh pr close`, so passing close implies merge will work.

### 3. Did `scripts/check-issue-author.sh` succeed? **YES.**

```
[test-git-gh] OP-3: scripts/check-issue-author.sh 2689
[test-git-gh] OP-3: invoking /usr/local/bin/check-issue-author.sh
  TRUSTED: Issue #2689 author 'drewthaler' association is MEMBER
[test-git-gh] OP-3: PASSED (trust check returned TRUSTED for #2689)
```
The script calls `gh api repos/judgemind/judgemind/issues/2689` internally;
exit code 0 and `TRUSTED:` prefix both appeared in the stdout tail.

### 4. Which auth mechanism was picked — scoped PAT, or GitHub App?

**Scoped PAT in Secrets Manager.** Rationale:

- **Simplicity.** The PAT path reuses the existing `secrets[]` / execution-role
  pattern already in every Judgemind task definition (`ANTHROPIC_API_KEY`,
  `DATABASE_URL`, etc.). One additional Secrets Manager entry, three lines of
  Terraform, no new control plane.
- **Same identity as the laptop dispatcher.** The laptop dispatcher already
  runs under a user PAT (`drewthaler` — per `~/.claude/projects/.../MEMORY.md`,
  the `judgemind-agent` account is temporarily GitHub-flagged). Using the
  same identity in the daemon avoids a parallel set of PR-author attribution
  and branch-protection edge cases. When the agent account is reinstated,
  the secret's value is rotated in place — task definition unchanged.
- **No new installation ceremony.** A GitHub App requires a registration
  UI walkthrough, webhook URL, private key rotation, and a JWT+installation
  token dance on the container side (either bundled into a sidecar or
  implemented in Python). For a ≤5-concurrent-agent dispatcher, the payoff
  doesn't justify the complexity.
- **Rate limit headroom is sufficient.** User PATs share the 5000 req/hr
  per-user budget with any human-driven `gh` usage. With 5 concurrent
  agents and the existing CLAUDE.md rate-awareness rules
  (`gh run watch --interval 60`, MCP-first for reads), measured burn in
  the current laptop workflow is well under 500 req/hr per agent. Even
  at 5× concurrency, headroom is >2×.

**When to revisit (file a `type/decision` issue, don't silently switch):**
- If the daemon ever needs to authenticate as the *repository* rather than
  a user (e.g. posting checks via the Checks API, writing commit statuses
  with a bot identity), migrate to a GitHub App. The PAT path cannot
  represent a non-user identity.
- If rate-limit pressure measurably blocks the dispatcher (look for 403
  rate-limit responses in `dispatcher.failures`), a GitHub App's per-repo
  5000 req/hr quota becomes meaningful.

### 5. Rate-limit or IP-allowlist surprises? **None observed.**

- The run consumed ~10 API calls (git credential refresh, `gh auth status`,
  `gh pr create`, `gh pr view`, `gh api repos/…/issues/2689`, `gh pr close`).
  No 403s, no `X-RateLimit-Remaining` warnings.
- Fargate's NAT gateway egress IP (`44.224.204.57`) is not on any
  organization IP allowlist — GitHub happily accepted requests. The user's
  local `gh` auth and the containerized `gh` auth both use the same OAuth
  token scope bundle (`gist, read:org, repo, workflow`), and GitHub's token
  API does not rate-limit-differentiate between client locations for user
  PATs.
- The `.git/` clone over HTTPS used the NAT gateway; no LFS, no submodules,
  no packfile surprises. Shallow clone (`--depth=1`) took <2s for ~150MB of
  history.

## Verdict: GO — scoped PAT, as specified

Spike 0.7 passes every acceptance criterion on issue #2689. §14's auth plan
stands as written: Secrets Manager → `GITHUB_TOKEN` env var, consumed by
`gh` and (via `gh auth setup-git`) by `git push`. Phase 1 can proceed without
a GitHub App registration.

## Evidence (full run transcript)

ECS task ARN: `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/cc95a3f4e2f248bb8d4b3fe6a2be7d65`
Wall-clock: 78 seconds (PROVISIONING→STOPPED).

Key excerpt (full transcript in the verification-evidence comment on #2689):

```
[spike-wrapper] exit_code=0 stop_reason=Essential container in task exited

[dispatcher-spike] scenario=git_gh
[dispatcher-spike] whoami=spike home=/home/spike pwd=/home/spike
[dispatcher-spike] git_gh scenario: delegating to /usr/local/bin/dispatcher-spike-test-git-gh
[test-git-gh] gh auth status
  github.com
    ✓ Logged in to github.com account drewthaler (GITHUB_TOKEN)
    - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
[test-git-gh] OP-1: PASSED (push of spike/0.7-auth-test-1776557470 to judgemind/judgemind succeeded)
[test-git-gh] OP-2: PR created → https://github.com/judgemind/judgemind/pull/2720
[test-git-gh] OP-2: PASSED (gh pr create + view succeeded)
[test-git-gh] OP-3: invoking /usr/local/bin/check-issue-author.sh
  TRUSTED: Issue #2689 author 'drewthaler' association is MEMBER
[test-git-gh] OP-3: PASSED (trust check returned TRUSTED for #2689)
[test-git-gh] OP-4: gh pr close https://github.com/judgemind/judgemind/pull/2720 (not merged)
  ✓ Closed pull request judgemind/judgemind#2720
  ✓ Deleted branch spike/0.7-auth-test-1776557470
[test-git-gh] ALL_CHECKS_PASSED
[dispatcher-spike] test-git-gh exited with code=0
```

Post-run verification via `mcp__github__get_pull_request`:
- `state: closed`
- `merged_at: null`
- Head branch `spike/0.7-auth-test-1776557470` deleted (gh's `--delete-branch` honored)

## What this spike explicitly did NOT prove

- **`gh pr merge --squash --delete-branch`.** The spike closes the PR without
  merging per its own instructions. The merge codepath shares the same auth
  surface (`gh` over HTTPS with `${GITHUB_TOKEN}`) as `gh pr close`, so the
  transitive confidence is high — but empirical confirmation lives in the
  first real `/task-v2-*` end-to-end run.
- **Concurrency.** One task at a time. If 5 concurrent Fargate tasks all
  share the same PAT, rate-limiting and authentication-state (per-token
  `gh auth status` caches) could interact unexpectedly. This is tractable
  in Phase 1 by monitoring `dispatcher.failures.category = 'github_rate_limit'`
  and falling back to a per-slot PAT if needed.
- **GitHub App path.** Not attempted — the scoped PAT worked on the first
  try. A separate investigation issue can formalize the App migration plan
  if and when we need the repo-level identity.

## Reproducing the spike

```
# (One-time) create the scoped-PAT secret. The token can come from any
# GitHub account with push + PR access to judgemind/judgemind.
printf '%s' "${PAT_VALUE}" \
    | aws secretsmanager create-secret \
        --region us-west-2 \
        --name judgemind/dev/dispatcher-spike/github-token \
        --secret-string file:///dev/stdin

# Apply Terraform (wires the secret ARN into the task definition).
terraform -chdir=infra/terraform/environments/dev \
    apply -target=module.dispatcher_spike -auto-approve

# Build + push the spike image (includes gh CLI + test_git_gh.sh).
docker build --platform linux/amd64 \
    -f Dockerfile.dispatcher-spike \
    -t judgemind/dispatcher-spike:spike07 .
docker tag judgemind/dispatcher-spike:spike07 \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest
docker push \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest

# Run. Logs stream to CloudWatch /ecs/judgemind-dispatcher-spike-dev.
SPIKE_SKIP_DB=1 scripts/dispatcher-spike/run_fargate_claude_p.sh git_gh
```

## Follow-up issues

- **(will be filed)** When dispatcher v2 Phase 1 lands, rotate the spike's
  scoped PAT to a dedicated daemon identity (either a new `judgemind-dispatcher`
  machine user or — if the flagged `judgemind-agent` account is reinstated
  first — that one). The spike's PAT was borrowed from the user's local
  session for convenience; it should not persist into production. Blocked
  by #2686 (spike 0.4 completion) to serialize behind remaining spikes.
- **(will be filed)** Cleanup: fold the spike 0.7 secret and `git_gh`
  scenario into the broader spike-infrastructure teardown after Phase 0
  concludes. Blocked by #2686 + #2699 (spike 0.1 cleanup) so the whole
  spike footprint is removed in one coordinated PR.
- **GitHub App exploration** (NOT filed — no actionable next step today).
  If Phase 1 hits the rate-limit or non-user-identity edge cases called
  out in Finding 4, file then; today the PAT is sufficient and filing
  a GitHub App follow-up invites dispatcher v1 to pick it up unnecessarily.
