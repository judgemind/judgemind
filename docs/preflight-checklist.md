# Agent Preflight Checklist

Machine-readable checklist of rules extracted from CLAUDE.md. Agents should validate proposed actions against this checklist before execution.

## Shell Command Rules

| ID | Rule | Pattern to Reject | Fix |
|----|------|-------------------|-----|
| SH-01 | No `$()` command substitution | `$(` in command | Run the inner command as a separate tool call; use the literal result in the next command |
| SH-02 | No heredocs | `<<EOF`, `<<'EOF'`, `<<-EOF` | Write content to `{worktree}/tmp/file.txt` with Write tool, then pass via `--body-file` or `-F` |
| SH-03 | No inline python `-c` | `python3 -c` or `python -c` | Write to `{worktree}/tmp/script.py`, then run the file |
| SH-04 | No quoted strings with `&&` or `;` | `"..."` or `'...'` combined with `&&` or `;` | Split into separate tool calls, one command per call |
| SH-05 | No `bash` prefix for scripts | `bash scripts/...` | Run directly: `scripts/start-worker.sh` |
| SH-06 | Use `git -C` for remote paths | `cd /path && git ...` | `git -C /absolute/path <subcommand>` |
| SH-07 | Temp files in worktree only | `/tmp/` in file paths | Use `{worktree}/tmp/` instead |

## File Operation Rules

| ID | Rule | Check |
|----|------|-------|
| FO-01 | Read before Write | If the file might already exist, Read it first — Write will fail otherwise |
| FO-02 | Prefer Edit over Write | For modifying existing files, use Edit (sends only the diff) |
| FO-03 | Use dedicated tools | Never use Bash for `cat`, `ls`, `grep`, `find` — use Read, Glob, Grep |

## Git Rules

| ID | Rule | Check |
|----|------|-------|
| GI-01 | Never commit to `main` directly | All work on worktree branches; changes go through PRs |
| GI-02 | Commit messages use conventional format | `feat(area): description (#N)` |
| GI-03 | Commit message via file | Write to `{worktree}/tmp/commit_msg.txt`, commit with `-F` |
| GI-04 | Never merge own PRs | Request review, wait for human merge |
| GI-05 | PR body must include `Closes #N` | Required for the unblock workflow |
| GI-06 | Push always followed by PR creation | Never push without immediately creating a PR |

## Pre-PR Check Rules

| ID | Rule | Commands |
|----|------|----------|
| PR-01 | Python lint | `.venv/bin/ruff check src/ tests/` |
| PR-02 | Python format | `.venv/bin/ruff format --check src/ tests/` |
| PR-03 | Python tests | `.venv/bin/pytest tests/ -v --tb=short` |
| PR-04 | TypeScript lint | `npm run lint` |
| PR-05 | TypeScript typecheck | `npm run typecheck` |
| PR-06 | TypeScript tests | `npm test` |
| PR-07 | TypeScript build (web) | `npm run build` (for `packages/web/` only) |
| PR-08 | Terraform format | `terraform fmt -check -recursive` |
| PR-09 | Terraform validate | `terraform init -backend=false && terraform validate` |

## Task Workflow Rules

| ID | Rule | Check |
|----|------|-------|
| TW-01 | Single issue per PR | Do not combine unrelated changes |
| TW-02 | Sync before implementing | `git fetch origin main && git rebase origin/main` |
| TW-03 | Watch CI to completion | `gh run watch` must exit before doing anything else |
| TW-04 | Clean up worktree when done | `scripts/end-worker.sh {worktree}` is the last step |
| TW-05 | Never deploy to production | Production deploys are human-only |
| TW-06 | Venv isolation per worktree | Never share venvs between worktrees |

## Security Rules

| ID | Rule | Check |
|----|------|-------|
| SE-01 | No hardcoded secrets | Use environment variables for API keys, credentials, URLs |
| SE-02 | No large binaries in git | Use `.gitignore` |
| SE-03 | No production scraping in dev | Only fetch pages for fixture creation |
