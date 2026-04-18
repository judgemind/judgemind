---
name: spike-hello
description: Throwaway test skill for dispatcher v2 spike 0.5 — cross-tool symlink sanity check. Tests whether Gemini CLI and OpenCode discover a skill under .agents/skills/ via a symlink to .claude/skills/. Use this skill to respond "hello from spike-hello" when invoked.
allowed-tools: Read
model: inherit
---

# spike-hello

This is a throwaway test skill created for GitHub issue #2687 (dispatcher v2 spike 0.5).

When this skill is invoked, respond with exactly:

```
hello from spike-hello (source: .claude/skills/spike-hello/SKILL.md)
```

Then stop. Do not perform any other actions. Do not read any other files. Do not invoke any tools beyond the response.

The purpose of this skill is purely to verify that multiple CLI runners (Claude Code, Gemini CLI, OpenCode) discover and invoke the skill through the `.agents/skills/spike-hello` symlink that points at this canonical location.
