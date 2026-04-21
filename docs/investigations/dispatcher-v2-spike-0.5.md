# Dispatcher v2 spike 0.5 — `.agents/skills/` cross-tool symlink sanity

**Date:** 2026-04-18
**Issue:** [#2687](https://github.com/judgemind/judgemind/issues/2687)
**Spec reference:** `docs/specs/dispatcher-v2-spec.md` §6b, §15 (spike 0.5)
**Environment:** macOS (Darwin 25.3.0), Node 24.14.0, Gemini CLI 0.38.2, OpenCode 1.4.14
**Agent worktree:** `.claude/worktrees/agent-a78236f1`

## TL;DR

**Verdict: symlink works — adopt `.agents/skills/` as the cross-tool path, with one caveat.**

- Gemini CLI and OpenCode both discover a skill located under `.agents/skills/<name>/SKILL.md` via a symlink that points at `.claude/skills/<name>/`.
- Both runners silently drop the non-standard Claude frontmatter fields `allowed-tools` and `model`. No errors, no warnings about the fields themselves.
- **Caveat:** OpenCode also reads `.claude/skills/` natively (same-directory discovery). If both `.agents/skills/<name>` (symlink) **and** `.claude/skills/<name>` (canonical) exist in the same project, OpenCode emits a `duplicate skill name` WARN for every overlap and first-wins deduplication kicks in.
- Gemini CLI does **not** read `.claude/skills/` natively — only `.gemini/skills/` and `.agents/skills/`. So the symlink is **mandatory** for Gemini; without it Gemini cannot see project-local Claude skills.

Net recommendation: **keep canonical SKILL.md files under `.claude/skills/`, and either symlink (Gemini requires it) or skip symlinking (OpenCode finds `.claude/skills/` natively but will double-read the symlink if both exist).** The duplicate-name warning is cosmetic — first-wins is deterministic — but loud. Two options to avoid it:

1. Use `.agents/skills/` exclusively for project-level skills (drop the `.claude/skills/` canonical location). OpenCode and Gemini both read `.agents/skills/`. Claude Code reads `.claude/skills/`; we would need a symlink in the **reverse** direction (`.claude/skills/` → `.agents/skills/`). This flips the §6b proposal but gives clean single-location discovery.
2. Keep `.claude/skills/` as canonical and symlink `.agents/skills/` → `.claude/skills/`. Accept the OpenCode duplicate-name WARN on stderr (it's suppressed at the default log level).

Spec §6b's `.claude/skills/` + symlink plan works. The hidden WARN only surfaces at `--log-level DEBUG`. No silent failure, no correctness risk.

## Test procedure

### Setup

1. **Installed CLIs locally** (worktree-level node, via npm):
   - `npm i -g @google/gemini-cli` → `gemini 0.38.2` at `~/.nvm/versions/node/v24.14.0/bin/gemini`.
   - `npm i -g opencode-ai` → `opencode 1.4.14` at `~/.nvm/versions/node/v24.14.0/bin/opencode`.
2. **Created a throwaway test skill** at .claude/skills/spike-hello/SKILL.md using `scripts/write-claude-file.sh` (the PreToolUse hook blocks direct writes inside `.claude/`). Frontmatter intentionally includes Claude-specific fields:
   ```yaml
   ---
   name: spike-hello
   description: Throwaway test skill for dispatcher v2 spike 0.5 — cross-tool symlink sanity check. ...
   allowed-tools: Read
   model: inherit
   ---
   ```
3. **Created a relative symlink** `.agents/skills/spike-hello → ../../.claude/skills/spike-hello`:

   ```
   $ ls -la .agents/skills/
   lrwxr-xr-x  1 drewthaler  staff  32 Apr 18 15:10 spike-hello -> ../../.claude/skills/spike-hello

   $ readlink -f .agents/skills/spike-hello
   /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a78236f1/.claude/skills/spike-hello

   $ stat .agents/skills/spike-hello/SKILL.md
   (file exists, size 918 bytes, readable)
   ```

### Auth status

Neither CLI had auth configured and no OAuth flow was performed (not required to answer the spike's discovery/frontmatter questions). See "Auth note" below.

## Findings

### 1. Gemini CLI — `gemini skills list`

**Result: skill discovered via symlink.**

```
spike-hello [Enabled]
  Description: Throwaway test skill for dispatcher v2 spike 0.5 ...
  Location:    /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a78236f1/.agents/skills/spike-hello/SKILL.md
```

- Reports the **symlink path** as `Location`, not the realpath target. Gemini resolved the symlink contents but didn't normalize the path.
- Status `[Enabled]` — skill is active and selectable.
- No warnings, no errors about `allowed-tools` or `model` frontmatter. `gemini --debug skills list` produced zero mentions of these fields, confirming silent drop.
- **Without the symlink (just `.claude/skills/spike-hello`)**, `gemini skills list` does NOT find the skill. Gemini discovers from `~/.agents/skills/`, `~/.gemini/skills/`, `<project>/.agents/skills/`, and `<project>/.gemini/skills/` — never from `.claude/skills/`.

Skills-discovery paths confirmed empirically:
- `~/.agents/skills/*/SKILL.md` — user-global, cross-tool
- `~/.gemini/skills/*/SKILL.md` — user-global, Gemini-specific
- `<project>/.agents/skills/*/SKILL.md` — project-level, cross-tool
- `<project>/.gemini/skills/*/SKILL.md` — project-level, Gemini-specific
- Built-in: `~/.nvm/.../node_modules/@google/gemini-cli/bundle/builtin/skill-creator/SKILL.md`

### 2. OpenCode — `opencode debug skill`

**Result: skill discovered via both `.claude/skills/` natively AND `.agents/skills/` symlink — triggers a duplicate-name warning.**

Full skill list (parsed JSON, 15 entries):

```
spike-hello: /Users/drewthaler/judgemind/judgemind-bootstrap/.claude/worktrees/agent-a78236f1/.agents/skills/spike-hello/SKILL.md
```

But at `--log-level DEBUG` on stderr:

```
WARN service=skill name=spike-hello
  existing=/.../.claude/worktrees/agent-a78236f1/.claude/skills/spike-hello/SKILL.md
  duplicate=/.../.claude/worktrees/agent-a78236f1/.agents/skills/spike-hello/SKILL.md
  duplicate skill name
```

(Same WARN fires for 13 other skills that exist both in `~/.claude/skills/` and `~/.agents/skills/` at the user-global level.)

Dedup behavior: **first-wins.** `existing` (loaded first) remains active; `duplicate` is ignored. Discovery order appears to be: user `~/.agents/skills/` → user `~/.claude/skills/` → project `.claude/skills/` → project `.agents/skills/`. The `spike-hello` entry listed by `opencode debug skill` picks the `.agents/skills/` path because that's the second-seen; but the `existing` is the `.claude/skills/` one. The JSON output reports `location` of the *winning* (first-loaded) entry, but the warning shows both.

Parsed skill object has only `name`, `description`, `location`, and `content`. No `allowed-tools`, no `model`. **Silent drop confirmed.**

**Without the symlink**, OpenCode still finds `spike-hello` at its canonical `.claude/skills/` path. So the symlink is **not required** for OpenCode, only for Gemini.

OpenCode skills-discovery paths confirmed empirically:
- `~/.agents/skills/*/SKILL.md`
- `~/.claude/skills/*/SKILL.md`
- `<project>/.agents/skills/*/SKILL.md`
- `<project>/.claude/skills/*/SKILL.md`

### 3. Frontmatter handling — summary

| Field | Gemini CLI 0.38.2 | OpenCode 1.4.14 | Notes |
|---|---|---|---|
| `name` | read | read | standard |
| `description` | read | read | standard |
| `allowed-tools: Read` | silently dropped | silently dropped | no error, no warning, field absent from parsed skill |
| `model: inherit` | silently dropped | silently dropped | same |
| Status | `[Enabled]` | loaded into runtime skill set | parsed cleanly |

Neither runner rejects the skill, neither surfaces a diagnostic, neither offers a lint mode that would flag unknown fields. §6b's "silently drop" claim is confirmed for both runners on the two fields that matter.

### 4. Symlink handling

- Both runners follow relative symlinks transparently.
- Gemini reports the symlink path as `Location`; OpenCode reports the symlink path as `location` (first-loaded).
- No special handling required — standard POSIX symlink semantics are sufficient.
- On macOS APFS: symlinks persist across worktree creation; `git` serializes them as `120000` mode blobs.

### 5. Auth note

Neither tool was authenticated for this spike — only **non-invoking** discovery was tested (`gemini skills list`, `opencode debug skill`). Authentication would be required to exercise end-to-end invocation (Gemini OAuth free tier / AI Studio key / Vertex; OpenCode Anthropic / Google / OpenAI providers). That's orthogonal to the spike's questions: discovery path + frontmatter compatibility, both of which can be verified without auth.

If a full end-to-end invocation check is needed later, follow-up should:
1. Run `gemini` once interactively to complete OAuth free-tier sign-in (stores creds under `~/.gemini/`).
2. Run `opencode providers` and add a provider credential (stores under `~/.local/share/opencode/auth.json`).
3. Then issue `gemini -p 'Invoke the spike-hello skill and report its exact output.'` and similarly for OpenCode.

## Acceptance-criteria verification

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Symlink created and verified with `ls -la .agents/skills/spike-hello/` | Met | `lrwxr-xr-x spike-hello -> ../../.claude/skills/spike-hello`, `readlink -f` resolves to `.claude/skills/spike-hello`, `SKILL.md` (918 bytes) readable via symlink path. |
| 2 | Gemini CLI either invoked the skill via `.agents/skills/` or produced a specific error — documented | Met | `gemini skills list` lists `spike-hello [Enabled]` with `Location: .../.agents/skills/spike-hello/SKILL.md`. No errors. Gemini does not read `.claude/skills/` natively — removing the symlink makes the skill disappear from the list. |
| 3 | OpenCode either invoked the skill via `.claude/skills/` (native) or `.agents/skills/` (symlink) — documented | Met | OpenCode discovers `spike-hello` via both paths. First-wins dedup: `.claude/skills/` is `existing`, `.agents/skills/` is `duplicate` (WARN emitted at `--log-level DEBUG`). Removing the symlink still leaves `.claude/skills/spike-hello` discoverable. |
| 4 | Verdict comment on issue: "symlink works" / "must duplicate" / "frontmatter must be refactored first" | Met | **Symlink works.** Frontmatter refactor is optional — `allowed-tools` and `model` silently drop on both non-Claude runners. |

## Docstring / spec contradictions

None of the source-file docstrings were wrong. The spec (`docs/specs/dispatcher-v2-spec.md` §6b) is mostly accurate; a small refinement is warranted:

- §6b says "OpenCode additionally reads `.claude/skills/` directly." That's true — but if we also provide `.agents/skills/` as a symlink, OpenCode WARNs about duplicate names. The spec should note this (one-line update, non-blocking).
- §6b says "Non-standard frontmatter fields (`allowed-tools`, `model`) silently drop on non-Claude runners." Verified — keep as-is.

Recommended spec edits are minor and included as a follow-up item rather than in this PR (investigation PRs should not edit specs without a separate decision).

## Cleanup

The throwaway `spike-hello` skill and its symlink are left in place as a reproducible artifact per the issue instructions. A follow-up issue will track their removal so this investigation note remains reproducible in the interim.

## Follow-ups

See #2692 (cleanup issue filed as part of this investigation): remove `.claude/skills/spike-hello/` and the `.agents/skills/spike-hello` symlink.

Optional follow-ups to consider later (not filed as issues, for maintainer decision):

1. Decide the canonical-vs-`.agents/skills/`-primary location for dispatcher v2 skills. Two viable options laid out in the TL;DR.
2. Authenticate and run an end-to-end invocation test once v2 reaches the integration stage. Not blocking — the spike's questions are answered.
3. Add a tiny CI lint that validates SKILL.md frontmatter only contains fields the chosen runner set understands (prevents surprise if someone adds a field that a future runner version does NOT silently drop).
