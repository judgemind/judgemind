#!/usr/bin/env python3
# venv: none
# permanent: true
"""Check and unwrap the framed result that dev_db_query_runner.py prints.

scripts/dev-db-query.sh pipes the raw ECS Exec capture into this script.
The SSM / ECS Exec transport cuts output off at ~1-10 KB and still
exits 0 (#4781, #3195), so the capture can hold partial JSON with nothing to
show it. The runner therefore frames every result (see the "Framing"
section of dev_db_query_runner.py). This script:

* finds the ``@@JM_DEVDB_BEGIN ...@@`` line and the ``@@JM_DEVDB_END@@``
  line;
* for an inline result, takes the payload between them;
* for an S3 result, downloads the object with ``aws s3 cp`` and deletes it
  afterwards (best effort; the bucket's ``tmp/`` lifecycle rule is the
  backstop);
* checks the payload's byte count and sha256 against the BEGIN line;
* prints the payload to stdout and exits 0.

Anything else (no BEGIN line, no END line, a short or altered payload, a
failed download) exits 1 with a message on stderr. It never prints a
partial payload to stdout.

Usage (called by dev-db-query.sh, not directly):
    printf '%s\\n' "$capture" | python3 scripts/dev_db_query_result.py [--region R]

Stdlib only: it runs on operator laptops and in the agent-runner image with
whatever ``python3`` is on PATH.

Exit codes:
    0   Complete result printed to stdout
    1   Result missing, truncated, or failed its integrity check
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass

# Must match dev_db_query_runner.py (test_dev_db_query_result.py checks).
FRAME_BEGIN = "@@JM_DEVDB_BEGIN"
FRAME_END = "@@JM_DEVDB_END@@"

_BEGIN_RE = re.compile(
    re.escape(FRAME_BEGIN)
    + r" (?P<mode>inline|s3) bytes=(?P<bytes>\d+) sha256=(?P<sha>[0-9a-f]{64})"
    + r"(?: uri=(?P<uri>s3://\S+?))?@@"
)

# Bound on how long the S3 download may take before we give up.
S3_DOWNLOAD_TIMEOUT_SECS = 120

FIX_BLOCK = """\
Fix: the result did not arrive complete over ECS Exec / SSM. Nothing was
printed to stdout, so no partial JSON can be mistaken for the full answer.
  - Re-run the same command; SSM drops are often transient.
  - If it keeps failing, run the query as a one-off Fargate task, which
    does not use ECS Exec:
      1. write a small psycopg script, e.g. tmp/q.py:
           import json, os, psycopg
           with psycopg.connect(os.environ["DATABASE_URL"]) as c:
               rows = c.execute("SELECT ...").fetchall()
               print(json.dumps(rows, default=str))
      2. scripts/ecs-run-task.sh tmp/q.py"""


class ResultError(Exception):
    """The capture did not hold a complete, verified result."""


@dataclass(frozen=True)
class Frame:
    """A parsed BEGIN line plus the inline payload (if any)."""

    mode: str
    size: int
    sha256: str
    uri: str | None
    inline_payload: str | None


def _normalise(capture: str) -> str:
    # The --interactive session runs over a pty, which turns "\n" into
    # "\r\n". The payload is compact ASCII JSON (json.dumps escapes
    # non-ASCII and newlines), so dropping every "\r" cannot alter it.
    return capture.replace("\r", "")


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def parse_frame(capture: str) -> Frame:
    """Parse the framed result out of a raw ECS Exec capture.

    Raises ``ResultError`` when the BEGIN or END marker is missing, or when
    an inline payload is not exactly as long as the BEGIN line says.
    """
    text = _normalise(capture)
    match = _BEGIN_RE.search(text)
    if match is None:
        detail = _tail(text) or "(no output)"
        raise ResultError(
            "no result frame in the ECS Exec output. The runner failed before "
            "it printed a result, or the stream was cut off before the first "
            f"line arrived. Output received:\n{detail}"
        )

    mode = match.group("mode")
    size = int(match.group("bytes"))
    sha = match.group("sha")
    uri = match.group("uri")

    # The BEGIN line ends with "@@" and then a newline.
    rest = text[match.end() :].removeprefix("\n")

    # rfind: the real END marker is the last one, even if a value in the
    # payload happens to contain the marker text.
    end_at = rest.rfind(FRAME_END)
    if end_at == -1:
        raise ResultError(
            f"the result was cut off: the {mode} frame has no end marker "
            f"(expected {size} bytes; received {len(rest.encode('utf-8'))} bytes "
            "after the begin marker). The SSM / ECS Exec stream ended early."
        )

    if mode == "s3":
        if uri is None:
            raise ResultError("the s3 result frame does not name an s3:// URI.")
        return Frame(mode, size, sha, uri, None)

    # The runner prints "<payload>\n" before the END marker.
    body = rest[:end_at].removesuffix("\n")
    return Frame(mode, size, sha, None, body)


def verify_payload(payload: bytes, frame: Frame) -> None:
    """Raise ``ResultError`` unless ``payload`` matches the frame's size and digest."""
    if len(payload) != frame.size:
        raise ResultError(
            f"the result is {len(payload)} bytes but the runner sent "
            f"{frame.size} bytes; it was truncated or altered in transit."
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != frame.sha256:
        raise ResultError(
            f"the result's sha256 is {digest} but the runner sent "
            f"{frame.sha256}; it was altered in transit."
        )


def download_s3(uri: str, region: str) -> bytes:
    """Fetch ``uri`` with ``aws s3 cp <uri> -``. Raise ``ResultError`` on failure."""
    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", uri, "-", "--region", region, "--only-show-errors"],
            capture_output=True,
            timeout=S3_DOWNLOAD_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResultError(
            f"downloading {uri} did not finish within {S3_DOWNLOAD_TIMEOUT_SECS}s."
        ) from exc
    except OSError as exc:
        raise ResultError(
            f"could not run the aws CLI to download {uri}: {exc}"
        ) from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise ResultError(
            f"downloading {uri} failed (aws exit {proc.returncode}): {err}"
        )
    return proc.stdout


def delete_s3(uri: str, region: str) -> None:
    """Best-effort delete of the staged result. Failures only print a note."""
    try:
        proc = subprocess.run(
            ["aws", "s3", "rm", uri, "--region", region, "--only-show-errors"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"Note: could not delete {uri}: {exc}", file=sys.stderr)
        return
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        print(
            f"Note: could not delete {uri} (the tmp/ lifecycle rule expires it): {err}",
            file=sys.stderr,
        )


def resolve_payload(capture: str, region: str) -> bytes:
    """Return the verified payload bytes from a raw capture.

    Raises ``ResultError`` if the result is missing, cut off, or altered.
    """
    frame = parse_frame(capture)
    if frame.mode == "inline":
        assert frame.inline_payload is not None
        payload = frame.inline_payload.encode("utf-8")
        verify_payload(payload, frame)
        return payload

    assert frame.uri is not None
    payload = download_s3(frame.uri, region)
    # On a mismatch verify_payload raises before the delete, which leaves
    # the object in place so the mismatch can be inspected.
    verify_payload(payload, frame)
    delete_s3(frame.uri, region)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args(argv)

    capture = sys.stdin.read()
    try:
        payload = resolve_payload(capture, args.region)
    except ResultError as exc:
        print(f"Error: dev-db-query result is incomplete: {exc}", file=sys.stderr)
        print(FIX_BLOCK, file=sys.stderr)
        return 1

    sys.stdout.write(payload.decode("utf-8"))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
