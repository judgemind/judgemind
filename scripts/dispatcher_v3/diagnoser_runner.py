#!/usr/bin/env python3
"""Diagnoser entrypoint for the dispatcher-v3 ECS task.

Invoked as ``python -m dispatcher_v3.diagnoser_runner`` by F2's
``diagnoser`` ECS task definition
(``infra/terraform/modules/dispatcher-v3-task-defs/main.tf``).

The launcher spawns this task whenever a task-runner ECS task exits
non-zero (or is killed by silent-hang detection); the failed agent's
``AGENT_ID`` is passed as a single env-var override on the diagnoser
container per spec §4.2. This wrapper:

1. Resolves ``AGENT_ID`` from the env (fail-loudly if missing — the
   launcher contract guarantees it is set).
2. Spawns ``claude -p "/diagnose-failure $AGENT_ID"`` with stream-json
   output and partial messages so the diagnoser SKILL's tool calls
   stream into CloudWatch Logs in real time (same shape as the
   task-runner — see ``dispatcher_v3.agent_runner``).
3. Tees the subprocess's stdout to a local jsonl session file so the
   archive is byte-for-byte identical to what CloudWatch saw.
4. On EXIT (success or failure) uploads two artifacts to S3:
   - ``s3://<sessions-bucket>/diagnoser-<agent_id>.jsonl`` — raw
     stream-json transcript.
   - ``s3://<sessions-bucket>/diagnoser-<agent_id>.txt`` — rendered
     compact transcript via ``/opt/judgemind-transcripts/render-transcript.py``
     (vendored into the image by ``Dockerfile.dispatcher-v3``).
5. Both uploads are best-effort (try/except, never raise) so the
   subprocess exit code propagates verbatim — the launcher's diagnoser-
   watch path keys off that exit code, not the upload result.

The ``diagnoser-`` prefix on both S3 keys is what distinguishes the
diagnoser session archive from the task-runner archive (which uses just
``<agent_id>.{jsonl,txt}``). Different prefix → no key collision when
the same agent's task-runner and diagnoser both upload session archives
for the same ``agent_id``.

Why a Python wrapper rather than running ``claude -p`` directly via the
ECS task-def ``command``? Two reasons:

- **Session capture.** The task-runner's archive contract (raw jsonl +
  rendered compact transcript on S3) is what makes the *task-runner*
  session diagnose-able. The same shape applied to the diagnoser makes
  *the diagnoser* itself diagnose-able if it crashes. Without this
  wrapper, a failed diagnoser leaves no archive — the next-agent retry
  loop has nothing to read except CloudWatch Logs (which can be missing
  on SIGKILL/OOM per spec §4.2).
- **Argv shape.** ``--output-format stream-json`` plus
  ``--include-partial-messages`` is a multi-arg invocation that is
  awkward to express in a Terraform JSON literal but trivial in Python.
  Centralizing it here keeps the task-def's ``command`` short and
  readable.

This module mirrors ``agent_runner.py`` deliberately — both runners
share the same EXIT/finally archive contract, so any future
generalisation (e.g. a shared ``_session_runner`` helper) has a clean
factoring target. We keep them as two thin top-level modules today
because the task-def ``command`` references each by name and a refactor
that breaks those references is a hard runtime failure (the regression
class this issue's CI guard catches).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import boto3

# ── Compact-transcript renderer location ─────────────────────────────────
# Path inside the dispatcher-v3 Docker image (set by F1's
# ``Dockerfile.dispatcher-v3`` COPY of ``vendor/judgemind-transcripts/``).
# Module constant so tests can patch it and so the location is documented
# in code rather than buried inside ``upload_archive``.
RENDER_TRANSCRIPT_SCRIPT = "/opt/judgemind-transcripts/render-transcript.py"


def _session_file(agent_id: str) -> Path:
    """Path to the raw stream-json diagnoser session log on the local fs.

    The ``diagnoser-`` prefix is what distinguishes this archive from
    the task-runner archive (which uses ``session-<agent_id>.jsonl``).
    Different prefix → no key collision when the same agent's
    task-runner and diagnoser both archive sessions for the same
    ``agent_id``.
    """
    return Path(f"/tmp/diagnoser-{agent_id}.jsonl")


def _compact_file(agent_id: str) -> Path:
    """Path to the rendered compact diagnoser transcript on the local fs."""
    return Path(f"/tmp/diagnoser-{agent_id}.txt")


def build_argv(agent_id: str) -> list[str]:
    """Return the argv list for ``claude -p /diagnose-failure <agent_id>``.

    Kept module-level so tests can pin the exact shape without going
    through ``main()``. The ``--output-format stream-json`` +
    ``--include-partial-messages`` pair matches ``agent_runner``'s claude
    invocation (see ``dispatcher_v3.runners.RUNNERS["claude"]``) — the
    diagnoser SKILL's tool calls stream into CloudWatch Logs in real
    time, which is also the liveness signal the launcher's diagnoser-
    watch path uses.
    """
    return [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        f"/diagnose-failure {agent_id}",
    ]


def upload_archive() -> None:
    """Upload the raw session jsonl + rendered compact transcript to S3.

    Best-effort: each upload (and the rendering step) is wrapped in its
    own try/except so a single failure does not skip the next step. Any
    exception is printed to stderr; nothing is re-raised. The function
    returns normally even if every step fails — by design, the caller
    (the launcher's diagnoser-watch path) tolerates missing archives
    (CloudWatch Logs is the SIGKILL-survivor fallback per spec §4.2).
    """
    agent_id = os.environ["AGENT_ID"]
    sessions_bucket = os.environ["SESSIONS_BUCKET"]
    session_file = _session_file(agent_id)

    if not session_file.exists():
        return

    # Step 1: raw jsonl upload.
    try:
        boto3.client("s3").upload_file(
            str(session_file), sessions_bucket, f"diagnoser-{agent_id}.jsonl"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        print(f"diagnoser-archive-upload-failed: {exc}", file=sys.stderr)

    # Step 2: render the compact transcript and upload it. Both render
    # and upload are inside the same try/except — if the renderer is
    # missing from the image (image-rebuild lag, dev environment), the
    # caller falls back to the raw jsonl uploaded above.
    compact = _compact_file(agent_id)
    try:
        # timeout=120: render-transcript.py is CPU-bound on the local
        # jsonl file; 2min covers a worst-case ~200MB session per the
        # judgemind-transcripts repo's measured ratios. The
        # check-subprocess-timeouts.sh hygiene rule requires a bounded
        # timeout on every subprocess.run call site under
        # scripts/**/*.py (#3213).
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
        boto3.client("s3").upload_file(
            str(compact), sessions_bucket, f"diagnoser-{agent_id}.txt"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        print(f"diagnoser-compact-transcript-upload-failed: {exc}", file=sys.stderr)


def main() -> int:
    """Spawn the diagnoser, tee stdout to the session log, return rc.

    stdout and stderr are merged (``stderr=subprocess.STDOUT``) into a
    single stream that is teed to both the parent process's stdout (the
    ECS log driver picks it up and ships to CloudWatch) and the local
    jsonl file (uploaded to S3 at exit). The duplicate is intentional —
    CloudWatch is the liveness signal, S3 is the archive (spec §4.2).
    """
    agent_id = os.environ["AGENT_ID"]
    argv = build_argv(agent_id)

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
