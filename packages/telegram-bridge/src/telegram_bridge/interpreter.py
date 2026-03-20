"""Claude-powered message interpreter for Telegram commands.

Replaces the hard-coded command parser with a single-turn Claude API call
that interprets all messages as free text, using the current orchestrator
status as context.

Two modes are available:

- **Haiku** (lightweight): Single-turn JSON classification.  Fast and cheap
  (~$0.001 per interaction).  Used for simple command classification.
- **Opus** (full agent): Multi-turn conversation with tool use.  Can read
  files, search code, query GitHub, and run allowlisted shell commands to
  answer substantive questions about the project.  Falls back to Haiku
  behaviour for orchestrator actions (start, stop, pause, resume).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from judgemind_config import DEFAULT_HAIKU_MODEL, DEFAULT_OPUS_MODEL

logger = logging.getLogger(__name__)

# Known action types and their required/optional fields.
# Each entry maps an action type to a dict of field specs:
#   {"field_name": {"required": bool, "type": type | tuple[type, ...], "default": value}}
_ACTION_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "start": {
        "issue": {"required": True, "type": int},
    },
    "stop": {
        "issue": {"required": True, "type": int},
    },
    "pause": {},
    "resume": {},
    "file_issue": {
        "description": {"required": True, "type": str},
        "priority": {"required": False, "type": str, "default": "p2"},
        "labels": {"required": False, "type": list},
    },
    "discuss": {
        "message": {"required": True, "type": str},
    },
    "do": {
        "instruction": {"required": True, "type": str},
    },
}

#: The set of known action type strings.
KNOWN_ACTION_TYPES: frozenset[str] = frozenset(_ACTION_SCHEMAS)

#: Allowed priority values for ``file_issue`` actions.
ALLOWED_PRIORITIES: frozenset[str] = frozenset({"p1", "p2", "p3"})

# The model to use for lightweight interpretation.  Haiku is fast and cheap.
_DEFAULT_MODEL = DEFAULT_HAIKU_MODEL

# The model to use for full agent mode with tool use.
OPUS_MODEL = DEFAULT_OPUS_MODEL

# Module-level client cache keyed by API key (or ``None`` for env-var default).
# This avoids creating a new HTTP connection pool on every interpreter call.
_client_cache: dict[str | None, anthropic.Anthropic] = {}


def get_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Return a cached :class:`anthropic.Anthropic` client for the given API key.

    If no client exists for this key yet, one is created and cached.  Passing
    ``None`` uses the SDK's default behavior (reads ``ANTHROPIC_API_KEY`` from
    the environment).

    Args:
        api_key: Anthropic API key, or ``None`` to use the environment variable.

    Returns:
        A reusable :class:`anthropic.Anthropic` client instance.
    """
    if api_key not in _client_cache:
        _client_cache[api_key] = anthropic.Anthropic(api_key=api_key)
    return _client_cache[api_key]


def clear_client_cache() -> None:
    """Clear the module-level client cache.

    Useful for testing or when API keys change at runtime.
    """
    _client_cache.clear()


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
- Use the orchestrator status context to give informed, specific answers.

### Confidence threshold — when to answer vs. forward

You must only answer directly when you have HIGH CONFIDENCE in BOTH:
(a) You fully understand the question, AND
(b) You have the data to answer it correctly and completely.

If either condition is not met, FORWARD to the orchestrator. Forwarding is \
always safe — answering incorrectly is not.

### NEVER do these things

- **NEVER ask the user for clarification.** If the intent is ambiguous, forward \
as a "discuss" action so the orchestrator (which has full codebase context) can \
figure it out. Do NOT reply with "Could you clarify..." or "What do you mean by...".
- **NEVER say "I don't know".** If you cannot answer, forward as a "discuss" \
action. Do NOT reply with "I'm not sure" or "I don't have that information".
- **NEVER guess.** If you are not confident in your answer, forward instead of \
guessing. A wrong answer is worse than forwarding.

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

3. **Requires codebase access or you are unsure?** If the question requires \
reading files, checking git history, inspecting code, debugging, understanding \
architecture, OR if you are not confident you can answer correctly — use a \
"discuss" action. When in doubt, forward. Examples: "Why is the OC scraper \
failing?", "What does the Scraper base class look like?", "Show me the latest \
changes to the API.", "What's our deployment strategy?"

4. **Requires the orchestrator to perform an action?** If the user wants something \
done that you cannot do (merge a PR, deploy, check CI logs, run tests, etc.), \
use a "do" action. Examples: "Merge PR #750", "Check CI on #738", "Deploy to \
dev", "Run the scraper tests."

If none of the above apply, just reply conversationally with an empty actions array. \
But remember: when uncertain, ALWAYS forward as "discuss" rather than guessing \
or asking for clarification.

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
    client: anthropic.Anthropic | None = None,
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
            Ignored when *client* is provided.
        client: Pre-created :class:`anthropic.Anthropic` client for connection
            reuse.  When ``None`` (the default), a cached client is obtained
            via :func:`get_client` keyed by *api_key*.
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

    if client is None:
        client = get_client(api_key=api_key)

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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from *text*, searching for the outermost braces.

    This handles cases where the model outputs preamble text before the JSON
    response (e.g. ``"Let me provide the answer:\\n{...}"``).  It scans for
    the first ``{`` and finds its matching ``}`` by tracking brace depth,
    respecting string literals.

    Returns the parsed dict, or ``None`` if no valid JSON object is found.
    """
    start = text.find("{")
    if start == -1:
        return None

    # Find the matching closing brace by tracking depth.  We need to
    # respect string literals to avoid being fooled by braces inside strings.
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    result = json.loads(candidate)
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    pass
                # If this brace pair didn't parse, keep looking for the next {.
                next_start = text.find("{", i + 1)
                if next_start == -1:
                    return None
                # Reset and try from the next {.
                return _extract_json_object(text[next_start:])
    return None


def _parse_response(text: str) -> dict[str, Any]:
    """Parse the Claude response as JSON, handling markdown code fences.

    The model sometimes wraps JSON in ```json ... ``` blocks, or outputs
    preamble text before the JSON response.  This function tries multiple
    strategies to extract the JSON:

    1. Strip markdown code fences and parse directly.
    2. Try to find and extract a JSON object embedded in the text.
    3. Fall back to treating the entire text as a plain-text reply.
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

    # Strategy 1: Try direct JSON parse.
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return _validate_parsed_response(result)
        return {"reply": str(result), "actions": []}
    except json.JSONDecodeError:
        pass

    # Strategy 2: Try to find an embedded JSON object (handles preamble text).
    extracted = _extract_json_object(cleaned)
    if extracted is not None and "reply" in extracted:
        logger.debug("Extracted JSON from text with preamble (length %d).", len(cleaned))
        return _validate_parsed_response(extracted)

    # Strategy 3: Fall back to treating the entire response as a reply.
    logger.warning("Failed to parse interpreter response as JSON: %s", text[:200])
    return {"reply": text.strip(), "actions": []}


def _validate_parsed_response(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and clean up a parsed JSON response dict.

    Validates the ``actions`` array entries against known schemas and
    returns the result with only valid actions retained.
    """
    # Validate actions.
    actions = result.get("actions", [])
    validated_actions: list[dict[str, Any]] = []
    if isinstance(actions, list):
        for action in actions:
            validated = _validate_action(action)
            if validated is not None:
                validated_actions.append(validated)

    result["actions"] = validated_actions
    return result


def _validate_action(action: Any) -> dict[str, Any] | None:
    """Validate a single action dict against the known schema.

    Returns the (possibly cleaned-up) action dict if valid, or ``None``
    if the action should be dropped.  Logs a warning for every dropped or
    fixed action.
    """
    if not isinstance(action, dict):
        logger.warning("Dropping non-dict action: %r", action)
        return None

    action_type = action.get("type")
    if not isinstance(action_type, str):
        logger.warning("Dropping action without string 'type': %r", action)
        return None

    if action_type not in KNOWN_ACTION_TYPES:
        logger.warning("Dropping action with unknown type %r", action_type)
        return None

    schema = _ACTION_SCHEMAS[action_type]

    # Check required fields and types.
    for field_name, spec in schema.items():
        value = action.get(field_name)
        if value is None:
            if spec.get("required", False):
                logger.warning(
                    "Dropping %r action: missing required field %r",
                    action_type,
                    field_name,
                )
                return None
            # Apply default for optional missing fields.
            if "default" in spec:
                action[field_name] = spec["default"]
            continue

        expected_type = spec.get("type")
        if expected_type is not None and not isinstance(value, expected_type):
            # Try to coerce int fields from string (e.g. "42" → 42).
            if expected_type is int and isinstance(value, str):
                try:
                    action[field_name] = int(value)
                    logger.warning(
                        "Coerced %r field %r from string %r to int",
                        action_type,
                        field_name,
                        value,
                    )
                    continue
                except ValueError:
                    pass
            logger.warning(
                "Dropping %r action: field %r has type %s, expected %s",
                action_type,
                field_name,
                type(value).__name__,
                expected_type.__name__ if isinstance(expected_type, type) else str(expected_type),
            )
            return None

    # Special validation: normalize priority for file_issue.
    if action_type == "file_issue":
        priority = action.get("priority", "p2")
        if priority not in ALLOWED_PRIORITIES:
            logger.warning(
                "Normalizing invalid priority %r to 'p2' in file_issue action",
                priority,
            )
            action["priority"] = "p2"

    return action


# Maximum tokens for the Opus agent response (needs more room for tool use).
_OPUS_MAX_TOKENS = 4096

# Maximum number of tool-use rounds before giving up.
_MAX_TOOL_ROUNDS = 10

_OPUS_SYSTEM_PROMPT = """\
You are an intelligent assistant for the Judgemind project — a free, \
open-source legal research platform. You communicate with the project \
maintainer via Telegram.

You have read-only access to the repository via tools. Use them to answer \
questions about the codebase, architecture, GitHub issues/PRs, git history, \
and database contents.

## What you CAN do (answer directly using tools)

- Read files from the repository
- Search code with grep or glob patterns
- Check GitHub issues and PRs (gh issue/pr list/view)
- View git log, diff, status, branches
- Query the dev database (scripts/dev-db-query.sh)
- Answer questions about project architecture, code patterns, and status

## What you CANNOT do (forward to orchestrator)

For these, return a JSON response with the appropriate action:
- **start** — Start work on an issue: {"type": "start", "issue": N}
- **stop** — Stop work on an issue: {"type": "stop", "issue": N}
- **pause** — Pause the orchestrator: {"type": "pause"}
- **resume** — Resume the orchestrator: {"type": "resume"}
- **file_issue** — Create a GitHub issue: \
{"type": "file_issue", "description": "...", "priority": "p2"}
- **do** — Perform an action (merge, deploy, run tests, etc.): \
{"type": "do", "instruction": "..."}

## Response format

Your final response MUST be valid JSON:
```json
{
  "reply": "Your answer to the user (plain text, no markdown)",
  "actions": []
}
```

The `actions` array should be empty for informational responses. Only include \
actions when the user explicitly requests an orchestrator command.

## Formatting Rules

- Write the reply in plain text only — no markdown, no bold, no bullet points, \
no code blocks. The message will be formatted for Telegram separately.
- Reference GitHub issues as #N (e.g. #42, #720). These will be automatically \
converted to clickable links.
- Be concise but thorough. Telegram messages should be readable on mobile.
- When showing code snippets, keep them short and relevant.
- Do NOT use asterisks for bold, underscores for italic, or backticks for code.
"""


def interpret_message_with_tools(
    *,
    text: str,
    repo_root: Path,
    orchestrator_status: dict[str, Any] | None = None,
    api_key: str | None = None,
    client: anthropic.Anthropic | None = None,
    model: str = OPUS_MODEL,
    rate_limiter: RateLimiter | None = None,
    max_tool_rounds: int = _MAX_TOOL_ROUNDS,
) -> InterpretedMessage:
    """Interpret a Telegram message using Claude Opus with tool access.

    This runs a multi-turn conversation loop: Claude can call tools
    (read files, search code, run allowlisted commands) to gather
    information before composing a final answer.

    The system prompt uses prompt caching to reduce costs on repeated
    calls (the system prompt is mostly static).

    Args:
        text: The raw message text from Telegram.
        repo_root: Absolute path to the repository root for tool access.
        orchestrator_status: Current orchestrator state.
        api_key: Anthropic API key (``None`` for env var default).
        client: Pre-created client for connection reuse.
        model: The Claude model to use.  Defaults to Opus.
        rate_limiter: Optional rate limiter.
        max_tool_rounds: Maximum number of tool-use rounds.

    Returns:
        An :class:`InterpretedMessage` with the reply text and any actions.

    Raises:
        RateLimitError: If the rate limit is exceeded.
        anthropic.APIError: If the API call fails.
    """
    from .tools import TOOL_DEFINITIONS, execute_tool

    if rate_limiter is not None:
        rate_limiter.acquire()

    if client is None:
        client = get_client(api_key=api_key)

    # Build system prompt with prompt caching.
    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _OPUS_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Build user message with orchestrator context.
    user_parts: list[str] = []
    if orchestrator_status:
        user_parts.append(
            "## Current Orchestrator Status\n```json\n"
            f"{json.dumps(orchestrator_status, indent=2, default=str)}\n```\n"
        )
    user_parts.append(f"## User Message\n{text}")
    user_message = "\n".join(user_parts)

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    # Multi-turn tool-use loop.
    for _round in range(max_tool_rounds):
        response = client.messages.create(
            model=model,
            max_tokens=_OPUS_MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

        # Check if the model wants to use tools.
        if response.stop_reason != "tool_use":
            # No more tool calls — extract final answer.
            break

        # Process tool calls and build tool results.
        assistant_content = response.content
        tool_results: list[dict[str, Any]] = []

        for block in assistant_content:
            if block.type == "tool_use":
                tool_output = execute_tool(
                    repo_root=repo_root,
                    tool_name=block.name,
                    tool_input=block.input,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    }
                )

        # Append assistant message and tool results to conversation.
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})
    else:
        # Exhausted tool rounds — use whatever we have.
        logger.warning("Opus agent exhausted %d tool rounds.", max_tool_rounds)

    # Extract the final text response.
    # Collect all text blocks so we can try them individually.  The model
    # sometimes outputs preamble/thinking in one text block and the JSON
    # response in another.  Trying individual blocks (last first, since
    # the final block is most likely to contain the answer) avoids the
    # concatenation problem that causes truncation and raw-JSON leaks.
    text_blocks: list[str] = []
    for block in response.content:
        if block.type == "text":
            text_blocks.append(block.text)

    if not text_blocks:
        # No text in the response — generate a fallback.
        return InterpretedMessage(
            reply="I looked into your question but couldn't formulate a response. "
            "Your message has been noted.",
            actions=[],
            raw_response={},
        )

    # Strategy 1: Try each text block individually (last first).
    # The final block most likely contains the JSON response.
    for block_text in reversed(text_blocks):
        parsed = _parse_response(block_text)
        if "reply" in parsed and parsed["reply"] != block_text.strip():
            # Successfully extracted a structured reply (not a raw-text fallback).
            return InterpretedMessage(
                reply=parsed.get(
                    "reply",
                    "I understood your message but couldn't generate a response.",
                ),
                actions=parsed.get("actions", []),
                raw_response=parsed,
            )

    # Strategy 2: Try the concatenated text (original behavior).
    response_text = "".join(text_blocks)
    parsed = _parse_response(response_text)
    return InterpretedMessage(
        reply=parsed.get("reply", "I understood your message but couldn't generate a response."),
        actions=parsed.get("actions", []),
        raw_response=parsed,
    )


def build_orchestrator_status(
    *,
    active_agents: list[dict[str, Any]] | None = None,
    open_prs: list[dict[str, Any]] | None = None,
    recently_completed: list[dict[str, Any]] | None = None,
    queue: list[dict[str, Any]] | None = None,
    paused: bool = False,
    stopped_issues: list[int] | None = None,
    prs_since_last_audit: int = 0,
    session_number: int = 0,
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
        prs_since_last_audit: Number of PRs merged since the last /audit run.
            The orchestrator triggers an audit when this reaches 20.
        session_number: How many times the orchestrator has been (re)started
            by the outer ``while :; do`` loop.  Incremented on each startup
            and persisted so the next session continues the count.

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
        "prs_since_last_audit": prs_since_last_audit,
        "session_number": session_number,
        "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
