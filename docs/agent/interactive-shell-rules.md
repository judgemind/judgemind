# Interactive Shell Rules — Agent Reference

## When to read this

Read this doc when you are running on an **operator laptop** (interactive Claude CLI session) and need to know which shell patterns the PreToolUse hook will block. These rules exist to prevent the CLI from stalling on permission prompts — a problem that does not exist in the Fargate dispatcher container.

If you are on Fargate (inside `Dockerfile.dispatcher`), only the four safety-critical rules in §Fargate scope below apply. The six interactive-prompt-prevention rules in §Shell NEVERs are inert in-container.

Cross-reference: `docs/agent/unattended-patterns.md` covers the broader set of permission-prompt workarounds (git, curl, secrets, `.claude/` writes, ECS, Telegram). This doc is the canonical home for the hook-enforced shell NEVERs.

---

## Shell NEVERs (operator laptops — hook-enforced)

The PreToolUse hook (`.claude/hooks/preflight-bash.sh`) blocks all of the following on operator laptops.

### Never use `$()` command substitution

`$()` in any Bash command triggers a permission prompt. Run the inner command as a separate Bash tool call and use the literal result in the next command. For secrets, use `scripts/with-secret.sh` — never `$(aws secretsmanager get-secret-value ...)`.

### Never use heredocs (`<<EOF`)

Write content to a file using the Write tool, then reference it with `--body-file` or `-F`. For commits: `git commit -F {worktree}/tmp/commit_msg.txt`. For PR bodies: `gh pr create --body-file {worktree}/tmp/body.txt`.

### Never use `python3 -c "..."` inline scripts

Write the script to `{worktree}/tmp/script.py` first, then run it as a file. Even a single-line script with semicolons counts. Inline scripts trigger a prompt and are hard to debug.

### Never combine quoted strings with `&&` or `;`

A hook rejects commands that contain quoted characters combined with `&&` or `;`. Split into separate tool calls.

### Never prefix scripts with `bash`

Run `scripts/cleanup_worktree.sh`, not `bash scripts/cleanup_worktree.sh`. The `Bash(scripts/*)` permission pattern only matches commands that start with `scripts/`; prepending `bash` breaks the match and triggers a prompt.

### Never use shell `&`, `nohup`, `disown`, or multicommand backgrounding tricks

These require compound commands that cannot be allowlisted and always trigger prompts. Use the Bash tool's `run_in_background: true` parameter instead — it runs the command as a background task natively, with no shell tricks needed.

### Never use Edit or Write tools on files inside `.claude/`

The CLI platform blocks Edit and Write operations on any path under `.claude/`. To modify files there:

1. Write the content to `{worktree}/tmp/` using the Write tool.
2. Copy into place: `scripts/write-claude-file.sh {worktree}/tmp/file.md {worktree}/.claude/target/file.md`

The script uses Python's `shutil.copy2()` internally, which bypasses the platform restriction. Do not use `cp` directly — it may also be blocked. This applies to skill definitions (`SKILL.md`), hook scripts, and any other file under `.claude/`. (Incident: #2440 — silent writes to the main repo bypassing PR workflow.)

### Never run bare `git stash pop` or `git stash apply`

`git stash` stores refs in `$GIT_DIR/refs/stash`, which is a single per-clone stack shared across every worktree. A bare `git stash pop` in worktree A can silently apply a stash created by worktree B — your edits get overwritten and another agent's WIP lands in your diff. (Incident: #2749.)

Always pop by explicit ref: run `git stash list` first, confirm `stash@{0}`'s subject contains your current branch, then `git stash pop stash@{0}`. Better yet, avoid stash entirely — use a throwaway commit (`git commit -am "WIP"`, do the thing, `git reset --soft HEAD~1`), which has no shared global state. The preflight hook blocks bare `pop` and `apply`.

---

## Fargate scope

**Four safety-critical rules remain enforced** in the Fargate dispatcher container (`Dockerfile.dispatcher`):

1. `git push` to `main` — never allowed anywhere.
2. `git worktree add` inside an existing worktree — creates orphaned child worktrees `cleanup_worktree.sh` cannot track (#2455).
3. Cross-worktree writes — `cp`/`mv`/`tar`/shell redirection from a worktree subagent into the main repo checkout (#2455, #2440).
4. Bare `git stash pop` / `git stash apply` — the stash list is per-clone, not per-worktree (#2749).

**Six interactive-prompt-prevention rules are inert in-container** (Fargate runs `--dangerously-skip-permissions`, no prompts):

- `$()` command substitution
- heredocs (`<<EOF`)
- inline `python3 -c "..."`
- quoted strings + `&&`/`;`
- `bash scripts/` prefix
- empty-quotes bypass

The Fargate image swaps the preflight hook at worktree-creation time to `scripts/preflight-bash-fargate.sh`, which enforces only the four safety-critical rules. This swap is scoped to the Fargate image via `DISPATCHER_FARGATE_HOOKS_DIR` + per-worktree `git update-index --skip-worktree`. Operator laptops keep the full ruleset. See issue #2982.

**Claim interlock note.** The `status/in-progress` label-only claim interlock (#2927, replacing the prior DB-row + label interlock #2866) is a workflow rule, not a shell rule — it applies in both environments.
