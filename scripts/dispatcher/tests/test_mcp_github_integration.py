"""Integration test: `mcp__github__*` tools execute inside `claude -p` (#2700).

Spike 0.1 (#2683) proved that `claude -p` on Fargate can **enumerate** the
``mcp__github__*`` suite when given an explicit ``--mcp-config``; it did
NOT prove the tools actually execute because no ``GITHUB_TOKEN`` was
provisioned at the time (see
``docs/investigations/dispatcher-v2-spike-0.1.md`` §4 bullet "The spike's
mcp-config.json references ${GITHUB_TOKEN} but the container did NOT
have GITHUB_TOKEN set"). Spike 0.7 (#2689) proved the scoped PAT in
Secrets Manager works for ``git`` and ``gh`` from inside the Fargate
task — but again, never actually invoked an ``mcp__github__*`` tool.

This test closes that remaining unknown. Structure:

**Preflight (always runs, including CI):** asserts the MCP config file
ships with the test fixture list, is valid JSON, and declares the
``github`` server with the expected command + args. Cheap to run, no
external dependencies.

**Full integration (gated):** only runs when all of the following are
true — ``CLAUDE_MCP_INTEGRATION_TEST=1``, ``GITHUB_TOKEN`` (or
``GITHUB_PERSONAL_ACCESS_TOKEN``) set, and the ``claude`` CLI present
on PATH. Anthropic auth is whatever ``claude`` picks up natively:
``ANTHROPIC_API_KEY`` in the env when present (the Fargate path —
wired via Secrets Manager), otherwise the local OAuth token from
``~/.claude/`` (the laptop-operator path). In that mode the test
spawns a real ``claude -p`` subprocess, asks it to call
``mcp__github__get_issue`` against issue #2683 (the spike 0.1 issue,
closed, ``type/spike`` label), and asserts:

1. The subprocess exits 0 (the spike 0.1 success-code signal from
   ``docs/investigations/dispatcher-v2-spike-0.1.md`` §2).
2. The output contains the expected issue title (proves the tool
   actually returned a structured response, not a generic "I couldn't
   call the tool" failure).
3. No 401 / 403 / "Invalid API key" markers appear anywhere in stdout
   or stderr — this is what would indicate the token reached the
   server but was rejected.

CI runs pytest with none of the gating env vars set, so the integration
section skips cleanly. Operators validating the Fargate wiring set
``CLAUDE_MCP_INTEGRATION_TEST=1`` + inject the scoped PAT (via
``scripts/with-secret.sh -e
GITHUB_TOKEN=judgemind/dispatcher/github-token``) before running
pytest, either locally (with ``claude`` on PATH) or inside the daemon
container image (same `claude` pinned at 2.1.114 per
``Dockerfile.dispatcher``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The closed test issue we interrogate. Picked because it is closed (will
#: not shift state under us), has a stable ``type/spike`` label, and its
#: title carries the exact substring we assert on.
TEST_ISSUE_NUMBER = 2683
TEST_ISSUE_TITLE_SUBSTRING = "claude -p end-to-end on Fargate"

#: Path to the shipped MCP config. Lives next to this test file so the
#: path is stable whether pytest runs from the repo root or from
#: scripts/dispatcher/.
MCP_CONFIG_PATH = Path(__file__).resolve().parent / "mcp-config.json"

#: Regexes that indicate the PAT was rejected by GitHub. Any of these
#: appearing in the subprocess output means the scoped-PAT → MCP server
#: handshake failed and we should fail the test loudly.
AUTH_ERROR_MARKERS = (
    "401",
    "403",
    "Invalid API key",
    "Bad credentials",
    "Requires authentication",
)


# --------------------------------------------------------------------------
# Preflight (always runs)
# --------------------------------------------------------------------------


class TestMcpConfigPreflight:
    """Assertions about the MCP config that ship with the test.

    These run in CI without any credentials — they guard against
    accidental drift (e.g. someone deleting the config file, or breaking
    its JSON, or renaming the ``github`` server). Breaking any of these
    breaks the integration test silently on Fargate, which is hard to
    detect because the log output there is much noisier than a CI run.
    """

    def test_config_file_exists(self) -> None:
        assert MCP_CONFIG_PATH.exists(), (
            f"MCP config fixture missing: {MCP_CONFIG_PATH}. "
            "Check that scripts/dispatcher/tests/mcp-config.json ships "
            "alongside this test."
        )

    def test_config_is_valid_json(self) -> None:
        data = json.loads(MCP_CONFIG_PATH.read_text())
        assert isinstance(data, dict)

    def test_declares_github_server_with_expected_package(self) -> None:
        data = json.loads(MCP_CONFIG_PATH.read_text())
        servers = data.get("mcpServers")
        assert isinstance(servers, dict), "mcpServers missing or not an object"
        github = servers.get("github")
        assert isinstance(github, dict), "github server missing from mcpServers"

        # The daemon's spike 0.1 config used exactly this command+package.
        # Keeping them pinned means the MCP server we load in the test is
        # the same one the real dispatcher subprocesses will load.
        assert github.get("command") == "npx"
        args = github.get("args", [])
        assert "@modelcontextprotocol/server-github" in args, (
            f"github server args must include @modelcontextprotocol/server-github, "
            f"got {args!r}"
        )

    def test_server_env_passthrough(self) -> None:
        """The server env must be empty (or explicit) so GITHUB_TOKEN is inherited.

        The ``@modelcontextprotocol/server-github`` package reads
        ``GITHUB_TOKEN`` / ``GITHUB_PERSONAL_ACCESS_TOKEN`` from its
        process env. If the MCP config hard-codes an ``env`` block that
        overrides these, the token the daemon fetches from Secrets
        Manager never reaches the MCP server and every tool call 401s.
        """
        data = json.loads(MCP_CONFIG_PATH.read_text())
        github_env = data["mcpServers"]["github"].get("env", {})
        assert isinstance(github_env, dict)
        # It is legal to set _unrelated_ env keys here; what we explicitly
        # forbid is the config taking over the auth variables and
        # clobbering the inherited scoped PAT.
        for banned in ("GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
            assert banned not in github_env, (
                f"MCP config must not set {banned} in mcpServers.github.env — "
                "the scoped PAT is supposed to flow through from the "
                "subprocess's environment (wired via Secrets Manager in "
                "the Fargate task definition, #2700)."
            )


# --------------------------------------------------------------------------
# Gating helpers (module-scoped so fixtures and test can share them)
# --------------------------------------------------------------------------


def _integration_mode_enabled() -> bool:
    """Return True when the caller has opted into the real subprocess run."""
    return os.environ.get("CLAUDE_MCP_INTEGRATION_TEST") == "1"


def _missing_integration_prereqs() -> list[str]:
    """List the prereqs the full integration run needs but doesn't see.

    ``ANTHROPIC_API_KEY`` is intentionally NOT on this list — it is only
    one of the two auth paths ``claude`` supports (the other being the
    laptop OAuth token in ``~/.claude/``). Flagging it here would force
    laptop operators to stub a fake API key to run the test, which adds
    nothing. On Fargate the daemon subprocess inherits
    ``ANTHROPIC_API_KEY`` from the task definition's ``secrets[]`` and
    the test works the same way.
    """
    missing: list[str] = []
    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get(
        "GITHUB_PERSONAL_ACCESS_TOKEN"
    ):
        missing.append("GITHUB_TOKEN (or GITHUB_PERSONAL_ACCESS_TOKEN)")
    if shutil.which("claude") is None:
        missing.append("`claude` CLI on PATH")
    return missing


# --------------------------------------------------------------------------
# Full integration (gated on CLAUDE_MCP_INTEGRATION_TEST=1)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not _integration_mode_enabled(),
    reason=(
        "Full integration run gated on CLAUDE_MCP_INTEGRATION_TEST=1. "
        "Set it plus GITHUB_TOKEN (Anthropic auth can be the local "
        "OAuth token or ANTHROPIC_API_KEY, whichever claude picks up) "
        "and run `pytest "
        "scripts/dispatcher/tests/test_mcp_github_integration.py -k integration` "
        "(locally or inside the daemon image) to exercise the real "
        "mcp__github__get_issue call against issue #2683."
    ),
)
class TestMcpGithubGetIssueIntegration:
    """Spawn `claude -p` and call mcp__github__get_issue against #2683.

    This is the test that actually closes the unknown spike 0.1 and 0.7
    left on the table: *can a `claude -p` subprocess running with the
    scoped PAT in its environment successfully invoke an
    ``mcp__github__*`` tool and get back a structured GitHub response?*
    """

    def _require_prereqs(self) -> None:
        missing = _missing_integration_prereqs()
        if missing:
            pytest.skip(
                f"Integration mode enabled but prerequisites missing: "
                f"{', '.join(missing)}. See the docstring on this test "
                "class for the required env vars."
            )

    def _run_claude(self, prompt: str) -> subprocess.CompletedProcess[str]:
        """Spawn `claude -p` with the test MCP config and return the result."""
        # `--` terminates option parsing so the prompt is never slurped
        # into the variadic `--mcp-config` list. Spike 0.1 caught this
        # trap; it is still the #1 way to misuse `claude -p --mcp-config`.
        argv = [
            "claude",
            "-p",
            "--max-turns",
            "5",
            "--output-format",
            "json",
            "--mcp-config",
            str(MCP_CONFIG_PATH),
            "--allow-dangerously-skip-permissions",
            "--",
            prompt,
        ]
        # subprocess.run with a 120-second cap — a real mcp__github__get_issue
        # call against a closed issue returns in well under a second on a
        # warm CLI; anything over 2 minutes means the CLI is stuck
        # (container crash, exhausted API budget, etc.) and the test
        # should fail rather than wait indefinitely.
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_get_issue_returns_expected_title(self) -> None:
        self._require_prereqs()

        prompt = (
            f"Call the mcp__github__get_issue tool with "
            f"owner='judgemind', repo='judgemind', "
            f"issue_number={TEST_ISSUE_NUMBER}. Then print the exact "
            f"title of the returned issue on a single line, nothing else."
        )
        result = self._run_claude(prompt)

        combined = f"{result.stdout}\n{result.stderr}"

        # 1. Exit code 0 is the success signal per spike 0.1 Finding 2.
        assert result.returncode == 0, (
            f"`claude -p` exited {result.returncode}. "
            f"stdout={result.stdout[:500]!r} stderr={result.stderr[:500]!r}"
        )

        # 2. No auth-error markers. A 401/403 from the GitHub API would
        # bubble up as an MCP tool result — which `claude -p` prints
        # verbatim, so the substrings would land in stdout.
        for marker in AUTH_ERROR_MARKERS:
            assert marker not in combined, (
                f"Auth-error marker {marker!r} found in claude -p output — "
                f"the scoped PAT appears to have been rejected. "
                f"stdout={result.stdout[:500]!r} stderr={result.stderr[:500]!r}"
            )

        # 3. The issue title substring is in the final text. We assert
        # on a stable substring (not the full title) so that tiny title
        # edits upstream don't break this test.
        assert TEST_ISSUE_TITLE_SUBSTRING in result.stdout, (
            f"Expected to see {TEST_ISSUE_TITLE_SUBSTRING!r} in claude -p "
            f"stdout, got stdout={result.stdout[:1000]!r}"
        )
