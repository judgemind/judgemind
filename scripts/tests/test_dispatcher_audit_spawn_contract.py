"""Contract tests for the /dispatcher SKILL.md `/audit` spawn block.

Regression test for #4091. Pre-fix, the dispatcher SKILL.md §Periodic Audit
section said "spawn `/audit` as a background subagent" without specifying
the Agent-tool prompt body. The dispatcher LLM passed bare
`prompt: "/audit"` to the Agent tool, and the spawned subagent's LLM
failed to recognise that as a slash-command invocation — replied with
"I don't have an `/audit` skill in my available-skills list" and exited
in 4-5 seconds with zero tool uses.

Reproduced 2026-05-05 in dispatcher session 1 at PR-merge counter 20
(after #4078 merged). Agent ID: `a5e5cb7ced6b52362`. Duration: 4.9s,
0 tool uses. The 20-PR threshold tripped silently — `/audit` was
effectively a no-op until #4091 landed.

Root cause was NOT that `audit` is missing from subagent skill
registries (it is loaded into every Agent-tool subagent's
`available-skills` list, verified 2026-05-08 from a `/task` subagent
spawned for #4091 itself). Root cause was the bare prompt: with no
context the spawned LLM did not invoke the Skill tool.

Fix path (a): make /audit reliably invocable from a subagent by
mandating an explicit, contentful spawn block in the dispatcher SKILL.md
that names the skill, references its SKILL.md path, and tells the
subagent to invoke it via the Skill tool. This test asserts the
post-fix invariants so the bare-prompt form cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHER_SKILL = REPO_ROOT / ".claude" / "skills" / "dispatcher" / "SKILL.md"


def _read_skill(path: Path) -> str:
    assert path.exists(), f"expected skill file to exist: {path}"
    return path.read_text(encoding="utf-8")


class TestDispatcherAuditSpawnPattern:
    """The dispatcher SKILL.md must spell out the /audit spawn pattern."""

    def test_skill_file_exists(self) -> None:
        assert DISPATCHER_SKILL.exists(), (
            f"/dispatcher SKILL.md must exist at {DISPATCHER_SKILL}"
        )

    def test_periodic_audit_section_present(self) -> None:
        """The §Periodic Audit section must exist — it is the trigger
        site for the every-20-PRs audit."""
        content = _read_skill(DISPATCHER_SKILL)
        assert "## Periodic Audit" in content, (
            "Dispatcher SKILL.md must keep the '## Periodic Audit' "
            "section that gates the every-20-PRs trigger."
        )

    def test_explicit_spawn_pattern_block_present(self) -> None:
        """The explicit spawn block for /audit must be present.

        The block heading is the load-bearing marker — without it,
        the dispatcher LLM falls back to bare `prompt: "/audit"`
        which is the #4091 failure mode.
        """
        content = _read_skill(DISPATCHER_SKILL)
        assert "### Spawn pattern" in content, (
            "Dispatcher SKILL.md must contain a '### Spawn pattern' "
            "subsection in §Periodic Audit. Without an explicit "
            "Agent-tool prompt block, the dispatcher LLM passes "
            'bare `prompt: "/audit"` and the spawn no-ops '
            "(see #4091)."
        )

    def test_spawn_block_references_4091(self) -> None:
        """The spawn block must reference #4091 so future readers
        can find the failure-mode evidence and reproduction."""
        content = _read_skill(DISPATCHER_SKILL)
        assert "#4091" in content, (
            "Dispatcher SKILL.md must reference #4091 — the issue "
            "documenting the bare-prompt failure mode and its repro."
        )

    def test_spawn_block_directs_skill_tool_invocation(self) -> None:
        """The spawn-block prompt must explicitly direct the spawned
        subagent to invoke the Skill tool with skill: \"audit\".

        This is the load-bearing instruction — the prompt must NOT
        rely on bare slash-command recognition by the spawned LLM.
        """
        content = _read_skill(DISPATCHER_SKILL)
        assert 'Skill tool with skill: "audit"' in content, (
            "Dispatcher SKILL.md spawn block must contain the literal "
            "string 'Skill tool with skill: \"audit\"' so the spawned "
            "subagent invokes the audit skill via the Skill tool "
            "rather than relying on bare slash-command recognition "
            "(the #4091 failure mode)."
        )

    def test_spawn_block_uses_isolation_worktree(self) -> None:
        """The spawn block must use `isolation: \"worktree\"` so the
        audit gets its own git worktree (matching /task spawn semantics)."""
        content = _read_skill(DISPATCHER_SKILL)
        # Find the Spawn pattern subsection and assert isolation appears
        # within it. We slice from the heading to the next '### ' heading
        # to avoid matching unrelated isolation: lines elsewhere in the
        # SKILL.md.
        marker = "### Spawn pattern"
        start = content.find(marker)
        assert start >= 0, "Spawn pattern subsection must exist"
        # Find the next subsection heading after the spawn-pattern start.
        next_section = content.find("\n### ", start + len(marker))
        block = content[start:next_section] if next_section >= 0 else content[start:]
        assert 'isolation: "worktree"' in block, (
            'Spawn pattern block must specify `isolation: "worktree"` '
            "so the audit subagent runs in its own git worktree."
        )

    def test_spawn_block_warns_against_bare_prompt(self) -> None:
        """The spawn block must explicitly call out that bare
        `prompt: \"/audit\"` is the regressed form — without that
        warning, a future editor might 'simplify' the block back into
        the failure mode."""
        content = _read_skill(DISPATCHER_SKILL)
        assert 'prompt: "/audit"' in content, (
            "Dispatcher SKILL.md must quote the regressed bare-prompt "
            'form `prompt: "/audit"` somewhere near the spawn block '
            "so the failure mode is documented inline."
        )
        # The warning context — a phrase that signals "this is what NOT
        # to do" — must accompany the quote.
        warning_phrases = [
            "is insufficient",
            "is a known failure mode",
            "bare-prompt",
            "bare prompt",
        ]
        assert any(phrase in content for phrase in warning_phrases), (
            "Dispatcher SKILL.md must accompany the quoted "
            '`prompt: "/audit"` form with a warning phrase '
            "(one of: %r) explaining why bare prompt is wrong." % warning_phrases
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
