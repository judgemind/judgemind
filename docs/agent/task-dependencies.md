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

## When you finish a task

**Implementation tasks (PRs):** dependent issues are unblocked automatically by the `unblock-issues` CI workflow when the PR merges. The PR body must include `Closes #N`.

**Non-PR completions:** unblock dependent issues by running `scripts/unblock-dependents.sh <your-issue>`. The script searches for open issues with `Blocked by #<your-issue>`, checks if all blockers are closed, and if so removes `status/blocked`, adds `agent/ready`, and cleans the `Blocked by` lines from the body. Use `--dry-run` to preview changes first.
