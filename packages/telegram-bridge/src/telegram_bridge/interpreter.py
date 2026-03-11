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
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# The model to use for interpretation.  Haiku is fast and cheap.
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

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
- **file_issue** — The user wants to create a GitHub issue. Extract the description, \
suggested priority (p1/p2/p3, default p2), and any area labels. The orchestrator will \
create the issue and confirm.
- **discuss** — The user wants to discuss something that requires codebase context \
(architecture, implementation, debugging, etc.). Forward the question to the \
orchestrator, which has full codebase access and can read files, check code, etc.
- **do** — The user wants the orchestrator to perform an action that you cannot do \
(e.g. "merge PR #750", "check CI on #738", "deploy", "run tests"). Forward the \
instruction to the orchestrator for execution.

## Response Format

Always respond with valid JSON in this exact schema:
```json
{
  "reply": "Your natural-language response to the user",
  "actions": [
    {"type": "start", "issue": 42},
    {"type": "pause"},
    {"type": "resume"},
    {"type": "stop", "issue": 99},
    {"type": "file_issue", "description": "Brief desc", "priority": "p2"},
    {"type": "discuss", "message": "The user's question"},
    {"type": "do", "instruction": "The action to perform"}
  ]
}
```

The `actions` array may be empty if no action is needed (just a reply).

## Guidelines

- Be concise — Telegram messages should be short and readable.
- If the user's intent is ambiguous, ask for clarification in the reply \
(with no actions).
- Use the orchestrator status context to give informed, specific answers.

### Deciding when to forward vs. answer directly

Use this decision tree in order:

1. **Answerable from the orchestrator status context?** If the question can be \
answered using the status JSON provided to you (active agents, open PRs, queue, \
recently completed tasks, paused state, stopped issues), answer directly with \
an empty `actions` array. Do NOT forward these as "discuss" or "do". Examples: \
"How many workers are active?", "What's the queue look like?", "Is #739 done \
yet?", "Is the orchestrator paused?", "Which issues were recently completed?"

2. **Orchestrator action requested?** If the user wants to start/stop an issue, \
pause/resume the orchestrator, or file an issue, use the corresponding action \
(start, stop, pause, resume, file_issue). Extract issue numbers where needed.

3. **Requires codebase access?** If the question requires reading files, checking \
git history, inspecting code, debugging, or understanding architecture — things \
not in the status JSON — use a "discuss" action. Examples: "Why is the OC \
scraper failing?", "What does the Scraper base class look like?", "Show me \
the latest changes to the API."

4. **Requires the orchestrator to perform an action?** If the user wants something \
done that you cannot do (merge a PR, deploy, check CI logs, run tests, etc.), \
use a "do" action. Examples: "Merge PR #750", "Check CI on #738", "Deploy to \
dev", "Run the scraper tests."

If none of the above apply, just reply conversationally with an empty actions array.

## Formatting Rules

- Write the reply in **plain text only** — no markdown, no bold, no bullet \
points, no code blocks. The message will be formatted for Telegram separately.
- Reference GitHub issues as `#N` (e.g. `#42`, `#720`). These will be \
automatically converted to clickable links.
- Do NOT use asterisks for bold, underscores for italic, or backticks for \
code. Just write plain text.
"""


class RateLimitError(Exception):
    """Raised when the Claude API rate limit is exceeded."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Try again in {retry_after:.0f} seconds.")


class RateLimiter:
    """Simple token-bucket rate limiter for Claude API calls.

    Allows at most ``max_calls`` invocations within a rolling ``window_seconds``
    window.  Thread-safe for single-process use (no cross-process locking).

    Args:
        max_calls: Maximum number of calls allowed within the window.
        window_seconds: Duration of the rolling window in seconds.
    """

    def __init__(self, max_calls: int = 1, window_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []

    def acquire(self) -> None:
        """Check the rate limit and record a new call.

        Raises:
            RateLimitError: If the rate limit would be exceeded.
        """
        now = time.monotonic()
        # Prune expired timestamps.
        cutoff = now - self.window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]

        if len(self._timestamps) >= self.max_calls:
            oldest = self._timestamps[0]
            retry_after = oldest + self.window_seconds - now
            raise RateLimitError(retry_after)

        self._timestamps.append(now)

    def reset(self) -> None:
        """Clear all recorded timestamps (useful for testing)."""
        self._timestamps.clear()


# Module-level default rate limiter: 20 calls per 60 seconds.
# Haiku calls cost ~$0.001 each, so even heavy usage is pennies.
_default_rate_limiter = RateLimiter(max_calls=20, window_seconds=60.0)


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
    rate_limiter: RateLimiter | None = _default_rate_limiter,
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
        rate_limiter: Optional rate limiter to prevent excessive API usage.
            Defaults to the module-level limiter (20 calls/60s).  Pass ``None``
            to disable rate limiting.

    Returns:
        An :class:`InterpretedMessage` with the reply text and any actions.

    Raises:
        RateLimitError: If the rate limit is exceeded.
        anthropic.APIError: If the API call fails.
        ValueError: If the response cannot be parsed as valid JSON.
    """
    if rate_limiter is not None:
        rate_limiter.acquire()

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
        A dict suitable for JSON serialization.  Includes an ``updated_at``
        ISO-8601 timestamp so consumers can detect staleness.
    """
    import datetime

    return {
        "active_agents": active_agents or [],
        "open_prs": open_prs or [],
        "recently_completed": recently_completed or [],
        "queue": queue or [],
        "paused": paused,
        "stopped_issues": stopped_issues or [],
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
