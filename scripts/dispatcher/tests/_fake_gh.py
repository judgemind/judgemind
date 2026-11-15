"""``FakeGh``: a recording fake ``gh`` for dispatcher tests (#5075).

The gh-isolate block in ``conftest.py`` fails any test whose code reaches
the ``gh`` shim. Tests that drive a ``gh``-calling daemon path use the
``fake_gh`` fixture (defined in ``conftest.py``, built on :class:`FakeGh`)
to declare and assert the calls instead.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

#: ``gh issue list`` argument patterns of the daemon's two scheduler-tick
#: scans (``_fetch_agent_ready_issues`` / ``_fetch_blocked_issues``).
GH_QUEUE_SCAN = (
    "issue list --repo judgemind/judgemind --label agent/ready --state open "
    "--json number,title,labels,createdAt --limit *"
)
GH_BLOCKED_SCAN = (
    "issue list --repo judgemind/judgemind --label status/blocked --state open "
    "--json number,title,labels,createdAt,body --limit *"
)
#: ``_check_gh_rate_limit``'s probe and a healthy answer to it.
GH_RATE_LIMIT_PROBE = "api rate_limit --jq .resources.core"
GH_RATE_LIMIT_OK = (
    '{"limit": 5000, "remaining": 4900, "reset": 4102444800, "used": 100}'
)


class FakeGh:
    """A fake ``gh`` first on PATH: records every call, answers from rules (#5075).

    The gh-isolate block fails any test whose code reaches the gh shim, so a
    test that drives a ``gh``-calling daemon path declares the calls it
    expects instead:

        fake_gh.expect("issue list --repo judgemind/judgemind --label status/blocked *",
                       stdout="[]")
        fake_gh.allow("api rate_limit *", stdout='{"remaining": 5000}')

    A pattern matches the call's arguments joined by single spaces (``"$*"``)
    and supports ``*`` only (any run of characters); every other character is
    literal. Rules are tried in order and the first match answers. After the
    test, the ``fake_gh`` fixture fails it when a call matched no rule or an
    ``expect`` rule was never called. ``calls`` / ``calls_matching`` return
    the recorded argument strings for finer assertions.

    The fake is a /bin/sh script, so the daemon's real subprocess, retry and
    JSON-parsing code runs against it.
    """

    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "bin"
        self._responses = root / "responses"
        self._log = root / "calls.log"
        self.bin_dir.mkdir(parents=True)
        self._responses.mkdir()
        self._log.touch()
        self._rules: list[dict[str, Any]] = []
        self._write_script()

    def expect(
        self, pattern: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0
    ) -> None:
        """Answer calls matching ``pattern``; fail the test if none is made."""
        self._add(pattern, stdout, stderr, exit_code, required=True)

    def allow(
        self, pattern: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0
    ) -> None:
        """Answer calls matching ``pattern``; the test need not make one."""
        self._add(pattern, stdout, stderr, exit_code, required=False)

    @property
    def calls(self) -> list[str]:
        """Every recorded call's arguments, in order (``"issue list ..."``)."""
        raw = self._log.read_text(encoding="utf-8", errors="replace")
        return [c for c in raw.split("\0") if c]

    def calls_matching(self, pattern: str) -> list[str]:
        """The recorded calls whose arguments match ``pattern``."""
        regex = self._regex(pattern)
        return [c for c in self.calls if regex.fullmatch(c)]

    def verify(self) -> None:
        """Raise when a call matched no rule or an ``expect`` rule went unused."""
        calls = self.calls
        unmatched = [c for c in calls if self._rule_for(c) is None]
        hit = {id(self._rule_for(c)) for c in calls}
        missing = [
            r["pattern"] for r in self._rules if r["required"] and id(r) not in hit
        ]
        problems = []
        if unmatched:
            problems.append(
                "gh calls with no fake_gh rule:\n"
                + "".join(f"  gh {c}\n" for c in unmatched)
            )
        if missing:
            problems.append(
                "expected gh calls that were never made:\n"
                + "".join(f"  gh {p}\n" for p in missing)
            )
        if problems:
            raise AssertionError(
                "fake_gh (#5075): "
                + "".join(problems)
                + "Recorded calls:\n"
                + "".join(f"  gh {c}\n" for c in calls)
            )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _regex(pattern: str) -> re.Pattern[str]:
        return re.compile(
            ".*".join(re.escape(p) for p in pattern.split("*")), re.DOTALL
        )

    def _rule_for(self, call: str) -> dict[str, Any] | None:
        for rule in self._rules:
            if rule["regex"].fullmatch(call):
                return rule
        return None

    def _add(
        self, pattern: str, stdout: str, stderr: str, exit_code: int, *, required: bool
    ) -> None:
        index = len(self._rules)
        (self._responses / f"{index}.out").write_text(stdout)
        (self._responses / f"{index}.err").write_text(stderr)
        self._rules.append(
            {
                "pattern": pattern,
                "regex": self._regex(pattern),
                "exit_code": int(exit_code),
                "required": required,
            }
        )
        self._write_script()

    def _write_script(self) -> None:
        def sq(text: str) -> str:
            return "'" + text.replace("'", "'\\''") + "'"

        lines = [
            "#!/bin/sh",
            "# fake gh from the fake_gh fixture (#5075). Never calls GitHub.",
            f"printf '%s\\000' \"$*\" >>{sq(str(self._log))}",
            'case "$*" in',
        ]
        for index, rule in enumerate(self._rules):
            case_pattern = "*".join(sq(p) for p in rule["pattern"].split("*"))
            out = sq(str(self._responses / f"{index}.out"))
            err = sq(str(self._responses / f"{index}.err"))
            lines.append(
                f"    {case_pattern}) cat {out}; cat {err} >&2; "
                f"exit {rule['exit_code']} ;;"
            )
        lines += [
            "esac",
            'echo "fake gh (#5075): no fake_gh rule matches: gh $*" >&2',
            "exit 97",
            "",
        ]
        script = self.bin_dir / "gh"
        tmp = self.bin_dir / ".gh.tmp"
        tmp.write_text("\n".join(lines))
        tmp.chmod(0o755)
        os.replace(tmp, script)
