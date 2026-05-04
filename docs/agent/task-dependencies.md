# Task Dependencies

How blocking and unblocking works across GitHub issues. CLAUDE.md contains a short summary; this doc has the full mechanics.

## The rules

- Blocked issues carry `status/blocked` and do **not** have `agent/ready`. Agents skip them.
- Dependencies are listed as `Blocked by #N` under a `## Dependencies` heading in the issue body.

## Marking an issue as blocked

**Both pieces are required** — the `status/blocked` label AND a `Blocked by #N` line in the issue body. The `unblock-issues` CI workflow searches for `Blocked by #N` in the body to find issues to unblock when a PR merges. If the label is present but the body text is missing, the workflow never fires and the issue stays stuck forever.

**Always use the helper script** to block an issue:

```
scripts/block-issue.sh <issue> <blocker>
```

This atomically:
1. Adds `Blocked by #<blocker>` to the issue body's `## Dependencies` section (creating it if absent)
2. Adds the `status/blocked` label
3. Removes the `agent/ready` label

**Do NOT** block issues by only adding the `status/blocked` label, only posting a comment, or using `Parent: #N` without a `Blocked by` line. These patterns break auto-unblocking:

| Pattern | Auto-unblock? | Correct? |
|---|---|---|
| `status/blocked` label + `Blocked by #N` in body | Yes | Yes |
| `status/blocked` label + comment "Blocked by #N" (no body text) | **No** | **No** |
| `Parent: #N` without `Blocked by #N` | **No** | **No** — `Parent:` is hierarchy, not dependency |
| `status/blocked` label only (no body reference at all) | **No** | **No** |

**Note:** `Parent: #N` expresses hierarchy (this is a sub-task of #N), not dependency. A sub-task can be worked on independently even while the parent is open. Only use `Blocked by #N` when the issue genuinely cannot proceed until #N is resolved.

### When the upstream blocker has no GitHub issue yet

A common /task failure mode: the agent discovers an upstream condition (billing depleted, secret rotated, infra-only failure) that does not yet have a GitHub issue tracking it. Posting a BLOCKED comment is necessary but not sufficient — without a tracker + `Blocked by #N` line, the issue stays `agent/ready` and the next agent re-investigates the same upstream condition (see #4035).

Use the helper:

```
scripts/block-on-new-issue.sh <dependent-issue> \
    --title "<conventional-commits style title>" \
    --body-file <path> \
    [--label <label> ...] \
    [--priority p0|p1|p2|p3]
```

This atomically:

1. Files a new tracker issue with the supplied title / body / labels.
2. Calls `block-issue.sh` to wire `Blocked by #<new>` + `status/blocked` on the dependent issue.
3. Prints both numbers (and URLs) to stdout.

**Do NOT pass `--label agent/ready`** for operator-only blockers (billing, secrets, account-level limits). Leaving an operator-only tracker `agent/ready` just sends another agent down the same dead end. Pick a `priority/p1` (workflow accelerator) or `priority/p0` (pipeline halted) label instead, and let the operator pick it up.

## When you finish a task

**Implementation tasks (PRs):** dependent issues are unblocked automatically by the `unblock-issues` CI workflow when the PR merges. The PR body must include `Closes #N`.

**Non-PR completions:** unblock dependent issues by running `scripts/unblock-dependents.sh <your-issue>`. The script searches for open issues with `Blocked by #<your-issue>`, checks if all blockers are closed, and if so removes `status/blocked`, adds `agent/ready`, and cleans the `Blocked by` lines from the body. Use `--dry-run` to preview changes first.

## Unblocking an issue manually

Use `scripts/unblock-issue.sh <N>` to manually clean up a specific blocked issue — for example, when one of its blockers has been closed out-of-band and the auto-unblock flow did not fire, or when a `Blocked by` line was added by mistake.

```
scripts/unblock-issue.sh <N>
scripts/unblock-issue.sh --dry-run <N>   # preview changes first
```

**Contrast with `unblock-dependents.sh`:**

| Script | Direction | Use when |
|---|---|---|
| `unblock-dependents.sh <closed-issue>` | Reverse: given a *closed* issue, finds everything blocked *by* it | You just finished work on issue X and want to unblock its dependents |
| `unblock-issue.sh <blocked-issue>` | Forward: given a *blocked* issue, prunes its stale blocker lines | You want to clean up a specific issue that may now be unblockable |

**Safety guarantees:**

- Only `Blocked by #N` lines for *closed* blockers are removed; open-blocker lines are never touched.
- The `status/blocked` → `agent/ready` label flip only fires when **all** blockers are closed (ALL-blockers gate).
- Use `--dry-run` to preview what would change before committing.

**Never** run `gh issue edit --remove-label status/blocked` directly — it skips the body cleanup and the ALL-blockers gate, leaving stale `Blocked by` lines that will confuse the auto-unblock CI workflow on the next related PR merge.
