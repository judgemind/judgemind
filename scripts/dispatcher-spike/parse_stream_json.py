#!/usr/bin/env python3
# venv: none (stdlib only)
# one-off: true
"""Parse `claude -p --output-format stream-json` log tail, compute peak context.

stream-json format (as of claude-code 2.1):
- Lines starting with '{' are events.
- Assistant turns have type="assistant" with message.usage containing:
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
- Final turn typically has type="result" with cumulative totals.

Peak context for a turn = input_tokens + cache_creation + cache_read.
This is the effective prompt size sent to the model that turn.

The output row (one line of JSONL appended to phase_measurements.jsonl):
    {"phase", "fixture", "input_tokens_first_turn", "peak_context_tokens",
     "output_tokens_total", "num_turns", "wall_clock_s", "exit_code"}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--phase", required=True)
    p.add_argument("--fixture", required=True)
    p.add_argument("--stdout-tail", required=True, help="path to stdout-tail file")
    p.add_argument("--run-json", required=True, help="path to run.json from wrapper")
    # The model identifier is metadata only — labels the measurement row so
    # mixed-runner experiments (spike 0.5 onward) can distinguish. Caller
    # passes the model alias they actually invoked claude -p with; no
    # hardcoded-models-check violation because this is a free-form label,
    # not a code path that sends an API request. Default kept intentionally
    # empty so the caller must be explicit.
    p.add_argument("--model", default="")
    args = p.parse_args()

    tail = Path(args.stdout_tail).read_text(encoding="utf-8", errors="replace")
    run = json.loads(Path(args.run_json).read_text(encoding="utf-8"))

    events: list[dict] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    first_turn_input = None
    peak_context = 0
    output_total = 0
    num_turns = 0

    for ev in events:
        if ev.get("type") == "assistant":
            usage = ev.get("message", {}).get("usage", {})
            inp = usage.get("input_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_creation_input_tokens", 0)
            out = usage.get("output_tokens", 0)
            ctx = inp + cache_read + cache_create
            if first_turn_input is None:
                first_turn_input = inp
            peak_context = max(peak_context, ctx)
            output_total += out
            num_turns += 1
        elif ev.get("type") == "result":
            # Some claude-code versions emit cumulative totals here.
            result_usage = ev.get("usage", {})
            if isinstance(result_usage, dict) and result_usage:
                peak_context = max(
                    peak_context,
                    result_usage.get("input_tokens", 0)
                    + result_usage.get("cache_read_input_tokens", 0)
                    + result_usage.get("cache_creation_input_tokens", 0),
                )
                output_total = max(output_total, result_usage.get("output_tokens", 0))

    # wall-clock: ended_at - started_at from the wrapper's run.json.
    from datetime import datetime

    def _parse(ts: str) -> float:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

    wall_s = None
    if run.get("started_at") and run.get("ended_at"):
        wall_s = round(_parse(run["ended_at"]) - _parse(run["started_at"]), 1)

    summary = {
        "phase": args.phase,
        "fixture": args.fixture,
        "model": args.model,
        "input_tokens_first_turn": first_turn_input,
        "peak_context_tokens": peak_context,
        "output_tokens_total": output_total,
        "num_turns": num_turns,
        "wall_clock_s": wall_s,
        "exit_code": run.get("exit_code"),
        "task_arn": run.get("task_arn"),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
