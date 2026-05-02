#!/usr/bin/env python3
"""Task-runner entrypoint for the dispatcher-v3 ECS task.

Invokes ``/task`` via the configured CLI runner (see ``dispatcher_v3.runners``),
streams stdout to a session log file at ``/tmp/session-<AGENT_ID>.jsonl`` so
the local file is the same bytes the ECS log driver ships to CloudWatch, and
on EXIT uploads two artifacts to S3:

1. The raw stream-json session file at ``s3://<SESSIONS_BUCKET>/<AGENT_ID>.jsonl``.
2. A compact rendered transcript (≈20:1 compression) at
   ``s3://<SESSIONS_BUCKET>/<AGENT_ID>.txt``, produced by
   ``/opt/judgemind-transcripts/render-transcript.py`` — bundled into the
   agent-runner image by the Dockerfile (issue F1).

The compact transcript is the diagnoser's primary read; the raw jsonl is the
fallback when the rendered form is missing or empty. See
``docs/specs/dispatcher-v3-spec.md`` §4.1 for the full contract.

Both uploads are best-effort: failures during EXIT cleanup print to stderr
but never raise, so the subprocess exit code propagates verbatim. The exit
code is also propagated when ``main()`` raises — the EXIT cleanup runs in a
``finally`` block before ``sys.exit(rc)``.

This module is intentionally small and dependency-light at import time
(only ``boto3`` plus ``dispatcher_v3.runners``) so the v3 task-runner image
and the unit-test environment can both import it. The dispatcher daemon and
its tests do not import this module — it is run as the ECS task entrypoint
(``python -m dispatcher_v3.agent_runner``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import boto3

from dispatcher_v3.runners import build_argv

# ── Required env vars ────────────────────────────────────────────────────
# Resolved lazily inside main()/upload_archive() so the module can be
# imported in tests without setting them. The ECS task definition exports
# all four; missing env at runtime is a hard error (KeyError) — see
# spec §4.1.

# ── Compact-transcript renderer location ─────────────────────────────────
# Path inside the agent-runner Docker image (set by Dockerfile F1). Module
# constant so tests can patch it and so the location is documented in code
# rather than buried inside ``upload_archive``.
RENDER_TRANSCRIPT_SCRIPT = "/opt/judgemind-transcripts/render-transcript.py"


def _session_file(agent_id: str) -> Path:
    """Path to the raw stream-json session log on the local filesystem."""
    return Path(f"/tmp/session-{agent_id}.jsonl")


def _compact_file(agent_id: str) -> Path:
    """Path to the rendered compact transcript on the local filesystem."""
    return Path(f"/tmp/session-{agent_id}.txt")


def upload_archive() -> None:
    """Upload the raw session jsonl + rendered compact transcript to S3.

    Best-effort: each upload (and the rendering step) is wrapped in its own
    try/except so a single failure does not skip the next step. Any
    exception is printed to stderr; nothing is re-raised. The function
    returns normally even if every step fails — by design, the diagnoser
    must tolerate missing artifacts (CloudWatch Logs is the SIGKILL-survivor
    fallback per spec §4.1).
    """
    agent_id = os.environ["AGENT_ID"]
    sessions_bucket = os.environ["SESSIONS_BUCKET"]
    session_file = _session_file(agent_id)

    if not session_file.exists():
        return

    # Step 1: raw jsonl upload.
    try:
        boto3.client("s3").upload_file(
            str(session_file), sessions_bucket, f"{agent_id}.jsonl"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        print(f"session-archive-upload-failed: {exc}", file=sys.stderr)

    # Step 2: render the compact transcript and upload it. Both render and
    # upload are inside the same try/except — if the renderer is missing
    # from the image (image-rebuild lag, dev environment), the diagnoser
    # falls back to the raw jsonl uploaded above.
    compact = _compact_file(agent_id)
    try:
        # timeout=120: render-transcript.py is CPU-bound on the local jsonl
        # file; 2min covers a worst-case ~200MB session per the
        # judgemind-transcripts repo's measured ratios. The check-subprocess-
        # timeouts.sh hygiene rule requires a bounded timeout on every
        # subprocess.run call site under scripts/**/*.py (#3213).
        subprocess.run(
            [
                "python",
                RENDER_TRANSCRIPT_SCRIPT,
                str(session_file),
                "--output",
                str(compact),
            ],
            check=True,
            timeout=120,
        )
        boto3.client("s3").upload_file(str(compact), sessions_bucket, f"{agent_id}.txt")
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        print(f"compact-transcript-upload-failed: {exc}", file=sys.stderr)


def main() -> int:
    """Run the configured runner, tee stdout to the session log, return rc.

    The runner-specific argv is built by ``dispatcher_v3.runners.build_argv``
    so adding a new runner does not require editing this entrypoint. stdout
    and stderr are merged (``stderr=subprocess.STDOUT``) into a single
    stream that is teed to both the parent process's stdout (the ECS log
    driver picks it up and ships to CloudWatch) and the local jsonl file
    (uploaded to S3 at exit). The duplicate is intentional — CloudWatch is
    the liveness signal, S3 is the archive (spec §4.1).
    """
    agent_id = os.environ["AGENT_ID"]
    issue_number = os.environ["TASK_ISSUE_NUMBER"]
    runner = os.environ.get("RUNNER", "claude")
    argv = build_argv(runner, issue_number, agent_id)

    session_file = _session_file(agent_id)
    with session_file.open("wb") as log:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        for chunk in iter(proc.stdout.readline, b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log.write(chunk)
            log.flush()
        return proc.wait()


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        upload_archive()
    sys.exit(rc)
