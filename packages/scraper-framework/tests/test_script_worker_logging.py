"""Worker log events must reach the log from every IngestionWorker driver.

``scripts/reingest_from_s3.py`` and ``scripts/rebuild_db.py`` run each S3
object through ``IngestionWorker.process_event`` (#4845).  The worker logs
through stdlib ``logging.getLogger``.  Before #4860 neither script routed
stdlib logging, so the worker's INFO events (``santa_clara_placeholder_skipped``,
``reingest_doc_timing``, ...) and their ``extra=`` fields were missing from
every reingest and rebuild oneshot.

Each case imports one entry point in a fresh interpreter (logging config is
process-global and set at import time), emits an INFO event with ``extra``
fields from a worker-module logger, and checks the JSON line on stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.abspath(os.path.join(_TESTS_DIR, "..", "src"))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_TESTS_DIR, "..", "..", "..", "scripts"))

# The probe runs in a child interpreter.  It imports the entry point, checks
# that a structlog ProcessorFormatter handler sits on the root logger, then
# logs a worker event.  The handler writes to the child's stderr.
_PROBE = textwrap.dedent(
    """
    import importlib
    import logging
    import sys

    import structlog

    sys.path[:0] = [{src!r}, {scripts!r}]
    importlib.import_module({module!r})

    bridged = [
        h for h in logging.root.handlers
        if isinstance(getattr(h, "formatter", None),
                      structlog.stdlib.ProcessorFormatter)
    ]
    if not bridged:
        sys.exit("no ProcessorFormatter handler on the root logger")

    logging.getLogger("ingestion.worker").info(
        "santa_clara_placeholder_skipped",
        extra={{"s3_key": "ca/santa_clara/superior_court/raw/abc.pdf",
                "page_count": 1}},
    )
    logging.getLogger("ingestion.worker").debug("debug_event_should_drop")
    """
)


def _run_probe(module: str) -> list[dict]:
    """Import *module* in a child interpreter, log a worker event, return JSON lines."""
    code = _PROBE.format(src=_SRC_DIR, scripts=_SCRIPTS_DIR, module=module)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"probe for {module} failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    records = []
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            records.append(json.loads(line))
    return records


@pytest.mark.parametrize(
    "module",
    [
        "ingestion.__main__",  # the live ingestion-worker service
        "reingest_from_s3",
        "rebuild_db",
    ],
)
def test_worker_info_event_with_extras_is_emitted(module: str) -> None:
    records = _run_probe(module)
    events = [r for r in records if r.get("event") == "santa_clara_placeholder_skipped"]
    assert len(events) == 1, f"{module}: worker INFO event missing; got {records!r}"
    event = events[0]
    assert event["level"] == "info"
    assert event["s3_key"] == "ca/santa_clara/superior_court/raw/abc.pdf"
    assert event["page_count"] == 1
    assert "timestamp" in event
    assert not any(r.get("event") == "debug_event_should_drop" for r in records)
