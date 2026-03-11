"""Claude-powered message interpreter for Telegram commands.

Replaces the hard-coded command parser with a single-turn Claude API call
that interprets all messages as free text, using the current orchestrator
status as context.

The interpreter uses ``claude-haiku-4-5`` for speed and cost efficiency
(~$0.001 per interaction).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# The model to use for interpretation.  Haiku is fast and cheap.
_DEFAULT_MODEL = "claude-haiku-4-5-20250514"

# Maximum tokens for the interpreter response.
_MAX_TOKENS = 512

_SYSTEM_PROMPT = """\
You are a lightweight command interpreter for the Judgemind orchestrator — \
an autonomous system that manages GitHub issues via worker agents.

You receive messages from a human operator via Telegram. Your job is to:
1. Understand the intent of the message.
2. Return a JSON response with a natural-language reply and any actions to take.

## Available Actions

- **reply** — Just respond to the user (no side effects). Use this for questions, \
status inquiries, greetings, or anything that doesn't require an orchestrator action.
- **start** — Queue an issue for the orchestrator to work on. Requires an issue number.
- **pause** — Pause the orchestrator (stop spawning new work).
- **resume** — Resume the orchestrator (start spawning work again).
- **stop** — Stop work on a specific issue. Requires an issue number.

## Response Format

Always respond with valid JSON in this exact schema:
```json
{
  "reply": "Your natural-language response to the user",
  "actions": [
    {"type": "start", "issue": 42},
    {"type": "pause"},
    {"type": "resume"},
    {"type": "stop", "issue": 99}
  ]
}
```

The `actions` array may be empty if no action is needed (just a reply).

## Guidelines

- Be concise — Telegram messages should be short and readable.
- When asked about status, summarize the orchestrator state provided to you.
- If the user asks to start or stop an issue, extract the issue number.
- If the user's intent is ambiguous, ask for clarification in the reply \
(with no actions).
- If the user asks about something outside your scope (code questions, \
debugging, etc.), politely note that you can only manage orchestrator \
operations and suggest they check the GitHub issue or logs directly.
- Use the orchestrator status context to give informed, specific answers.
"""


@dataclass(frozen=True)
class InterpretedMessage:
    """Result of interpreting a Telegram message via Claude API."""

    reply: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)


def interpret_message(
    *,
    text: str,
    orchestrator_status: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str = _DEFAULT_MODEL,
) -> InterpretedMessage:
    """Interpret a Telegram message using the Claude API.

    This makes a single-turn, synchronous API call to Claude Haiku to
    interpret the user's message in the context of the current orchestrator
    state.

    Args:
        text: The raw message text from Telegram.
        orchestrator_status: Current orchestrator state (agents, PRs, queue,
            paused status).  Passed as context to the interpreter.
        api_key: Anthropic API key.  If ``None``, the ``ANTHROPIC_API_KEY``
            environment variable is used (standard anthropic SDK behavior).
        model: The Claude model to use.  Defaults to Haiku for speed/cost.

    Returns:
        An :class:`InterpretedMessage` with the reply text and any actions.

    Raises:
        anthropic.APIError: If the API call fails.
        ValueError: If the response cannot be parsed as valid JSON.
    """
    client = anthropic.Anthropic(api_key=api_key)

    # Build the user message with orchestrator context.
    user_parts: list[str] = []
    if orchestrator_status:
        user_parts.append(
            "## Current Orchestrator Status\n```json\n"
            f"{json.dumps(orchestrator_status, indent=2, default=str)}\n```\n"
        )
    user_parts.append(f"## User Message\n{text}")
    user_message = "\n".join(user_parts)

    response = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract the text content from the response.
    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text = block.text
            break

    # Parse the JSON response.
    parsed = _parse_response(response_text)
    return InterpretedMessage(
        reply=parsed.get("reply", "I understood your message but couldn't generate a response."),
        actions=parsed.get("actions", []),
        raw_response=parsed,
    )


def _parse_response(text: str) -> dict[str, Any]:
    """Parse the Claude response as JSON, handling markdown code fences.

    The model sometimes wraps JSON in ```json ... ``` blocks.
    """
    cleaned = text.strip()

    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag).
        first_newline = cleaned.index("\n")
        cleaned = cleaned[first_newline + 1 :]
        # Remove closing fence.
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse interpreter response as JSON: %s", text[:200])
        # Fall back to treating the entire response as a reply.
        return {"reply": text.strip(), "actions": []}

    if not isinstance(result, dict):
        return {"reply": str(result), "actions": []}

    # Validate actions.
    actions = result.get("actions", [])
    validated_actions: list[dict[str, Any]] = []
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and "type" in action:
                validated_actions.append(action)

    result["actions"] = validated_actions
    return result


def build_orchestrator_status(
    *,
    active_agents: list[dict[str, Any]] | None = None,
    open_prs: list[dict[str, Any]] | None = None,
    recently_completed: list[dict[str, Any]] | None = None,
    queue: list[dict[str, Any]] | None = None,
    paused: bool = False,
    stopped_issues: list[int] | None = None,
) -> dict[str, Any]:
    """Build the orchestrator status dict for the interpreter context.

    This is the canonical structure that the orchestrator should write to
    ``tmp/orchestrator_status.json`` after every state change, and that the
    responder daemon reads to provide context to the Claude interpreter.

    Args:
        active_agents: List of active agent snapshots (issue, worker, phase).
        open_prs: List of open PRs (number, CI status, mergeable).
        recently_completed: List of recently completed tasks.
        queue: Next issues by priority.
        paused: Whether the orchestrator is paused.
        stopped_issues: Issue numbers that have been stopped.

    Returns:
        A dict suitable for JSON serialization.
    """
    return {
        "active_agents": active_agents or [],
        "open_prs": open_prs or [],
        "recently_completed": recently_completed or [],
        "queue": queue or [],
        "paused": paused,
        "stopped_issues": stopped_issues or [],
    }
