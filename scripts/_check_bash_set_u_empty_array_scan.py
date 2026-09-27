#!/usr/bin/env python3
# venv: none
# permanent: true
"""Scanning core for ``scripts/check-bash-set-u-empty-array.sh`` (#4720).

The wrapper used to walk every ``scripts/**/*.sh`` line by line in pure
bash (``while (( i < nlines ))`` loops with ``[[ =~ ]]`` per line and per
array name). That took ~10s of CPU on an idle laptop and dominated the
parallel ``scripts/run-ci-guards.sh`` wall time. This module is a
line-for-line port of the same three passes, so the verdicts are
identical. The wrapper keeps the CLI, the file discovery, the report
text and ``--fix``.

Input: NUL-separated file paths on stdin (already sorted by the wrapper).
Output: one record per line on stdout, bytes-exact:

    R<TAB><text>                    — a report line, printed verbatim
    A                               — one shape (A) violation
    B<TAB><line><TAB><name><TAB><file> — one shape (B) violation, for --fix

Regexes mirror the bash ERE originals. They run on bytes so ``\\s``
means exactly ``[[:space:]]`` in the C locale and file contents
round-trip without decoding.
"""

from __future__ import annotations

import re
import sys

EXCLUDE_SUFFIXES = (
    b"scripts/check-bash-set-u-empty-array.sh",
    b"scripts/tests/test_check_bash_set_u_empty_array.sh",
)

SP = rb"[ \t\n\v\f\r]"  # POSIX [[:space:]]

COMMENT_RE = re.compile(rb"^" + SP + rb"*#")
NOUNSET_RE = re.compile(
    rb"^"
    + SP
    + rb"*set"
    + SP
    + rb"+(-[a-zA-Z]*u[a-zA-Z]*("
    + SP
    + rb"|$)|-o"
    + SP
    + rb"+nounset)"
)
DECLARE_BARE_RE = re.compile(
    rb"^"
    + SP
    + rb"*(declare|typeset)"
    + SP
    + rb"+-[a-zA-Z]*a"
    + SP
    + rb"+([A-Za-z_][A-Za-z0-9_]*)("
    + SP
    + rb"|$)"
)
EMPTY_INIT_RE = re.compile(
    rb"^" + SP + rb"*([A-Za-z_][A-Za-z0-9_]*)=\(" + SP + rb"*\)" + SP + rb"*$"
)

ELIF_RE = re.compile(rb"^elif(" + SP + rb"|$)")
OPENER_RE = re.compile(rb"^(if|while|until|for)(" + SP + rb"|$)")
CASE_RE = re.compile(rb"^case(" + SP + rb"|$)")
FI_RE = re.compile(rb"^fi(" + SP + rb"|;|$)")
ESAC_RE = re.compile(rb"^esac(" + SP + rb"|;|$)")
DONE_RE = re.compile(rb"^done(" + SP + rb"|;|$)")
STOP_RE = re.compile(rb"^" + SP + rb"*(exit|return)(" + SP + rb"|$)")


def _closer_res(closer: bytes) -> tuple[re.Pattern[bytes], re.Pattern[bytes]]:
    tail = rb"(" + SP + rb"|;|$)"
    return (
        re.compile(rb";" + SP + rb"*" + closer + tail),
        re.compile(SP + closer + tail),
    )


CLOSER_RES = {c: _closer_res(c) for c in (b"fi", b"done", b"esac")}


def _one_liner(trimmed: bytes, closer: bytes) -> bool:
    a, b = CLOSER_RES[closer]
    return bool(a.search(trimmed) or b.search(trimmed))


def _guard_re(name: bytes, tests: bytes) -> re.Pattern[bytes]:
    return re.compile(
        rb"^"
        + SP
        + rb"*if"
        + SP
        + rb"+\[\[?"
        + SP
        + rb'+"?\$\{#'
        + name
        + rb'\[[@*]\]\}"?'
        + SP
        + rb"+("
        + tests
        + rb")"
        + SP
        + rb"+\]\]?"
        + SP
        + rb"*;?"
        + SP
        + rb"*then"
    )


NONEMPTY_TESTS = b"-gt" + SP + b"+0|-ge" + SP + b"+1|-ne" + SP + b"+0|!=" + SP + b"+0"
EMPTY_TESTS = b"-eq" + SP + b"+0|-lt" + SP + b"+1|-le" + SP + b"+0|==" + SP + b"+0"


def read_lines(path: bytes) -> list[bytes]:
    """Split like bash's ``while IFS= read -r line || [[ -n $line ]]``."""
    with open(path, "rb") as fh:
        data = fh.read()
    data = data.replace(b"\0", b"")  # bash variables cannot hold NUL
    if not data:
        return []
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    return lines


def lstrip_blank(s: bytes) -> bytes:
    return s.lstrip(b" \t")


def compute_depths(lines: list[bytes]) -> tuple[list[int], list[int]]:
    depth_at: list[int] = []
    branch_at: list[int] = []
    cur = 0
    cur_branch = 0
    for text in lines:
        trimmed = lstrip_blank(text)
        if not trimmed or trimmed.startswith(b"#"):
            depth_at.append(cur)
            branch_at.append(cur_branch)
            continue
        delta = 0
        bdelta = 0
        if ELIF_RE.search(trimmed):
            pass
        else:
            m = OPENER_RE.search(trimmed)
            if m:
                opener = m.group(1)
                closer = b"fi" if opener == b"if" else b"done"
                if not _one_liner(trimmed, closer):
                    delta = 1
                    if opener == b"if":
                        bdelta = 1
            elif CASE_RE.search(trimmed):
                if not _one_liner(trimmed, b"esac"):
                    delta = 1
                    bdelta = 1
            elif FI_RE.search(trimmed) or ESAC_RE.search(trimmed):
                delta = -1
                bdelta = -1
            elif DONE_RE.search(trimmed):
                delta = -1
        cur = max(cur + delta, 0)
        cur_branch = max(cur_branch + bdelta, 0)
        depth_at.append(cur)
        branch_at.append(cur_branch)
    return depth_at, branch_at


def scan_file(path: bytes, out: list[bytes]) -> None:
    lines = read_lines(path)
    n = len(lines)
    if n == 0:
        return
    is_comment = [bool(COMMENT_RE.search(line)) for line in lines]

    if not any(not c and NOUNSET_RE.search(line) for line, c in zip(lines, is_comment)):
        return

    # Shape (A): bare ``declare -a <name>`` read before assign.
    for decl_idx, dline in enumerate(lines):
        if is_comment[decl_idx]:
            continue
        m = DECLARE_BARE_RE.search(dline)
        if not m:
            continue
        name = m.group(2)
        assign_re = re.compile(rb"(^|[^A-Za-z0-9_])" + name + rb"\+?=\(")
        read_re = re.compile(rb"\$\{#?" + name + rb"\[")
        for scan_idx in range(decl_idx + 1, n):
            if is_comment[scan_idx]:
                continue
            sline = lines[scan_idx]
            if assign_re.search(sline):
                break
            if read_re.search(sline):
                out.append(
                    b"R\t  [declare -a " + name + b" read before assign under set -u]"
                )
                out.append(
                    b"R\t    "
                    + path
                    + b":"
                    + str(decl_idx + 1).encode()
                    + b": "
                    + dline
                )
                out.append(
                    b"R\t    "
                    + path
                    + b":"
                    + str(scan_idx + 1).encode()
                    + b": "
                    + sline
                )
                out.append(
                    b"R\t    fix: replace 'declare -a "
                    + name
                    + b"' with '"
                    + name
                    + b"=()'"
                )
                out.append(b"A")
                break

    # Shape (B): bare-empty ``<name>=()`` iterated while still empty.
    depth_at, branch_at = compute_depths(lines)
    for init_idx, iline in enumerate(lines):
        if is_comment[init_idx]:
            continue
        m = EMPTY_INIT_RE.search(iline)
        if not m:
            continue
        iname = m.group(1)
        base_branch = branch_at[init_idx]
        iassign_re = re.compile(rb"(^|[^A-Za-z0-9_])" + iname + rb"\+?=\(")
        iread_re = re.compile(rb"\$\{" + iname + rb"\[[@*]\]\}")
        iguarded_re = re.compile(rb"\$\{" + iname + rb"\[[@*]\]\+")
        len_guard_re = _guard_re(iname, NONEMPTY_TESTS)
        early_exit_re = _guard_re(iname, EMPTY_TESTS)

        verdict = b""
        verdict_idx = -1
        len_guard_open = -1
        in_early = False
        early_open = -1
        early_stop = False
        for scan_idx in range(init_idx + 1, n):
            if is_comment[scan_idx]:
                continue
            isline = lines[scan_idx]
            idepth = depth_at[scan_idx]

            if len_guard_open >= 0 and idepth <= len_guard_open:
                len_guard_open = -1
            if len_guard_re.search(isline):
                len_guard_open = idepth - 1

            if in_early and idepth <= early_open:
                if early_stop:
                    verdict = b"assigned"
                    break
                in_early = False
                early_open = -1
                early_stop = False
            if early_exit_re.search(isline):
                in_early = True
                early_open = idepth - 1
                early_stop = False
            if in_early and idepth > early_open and STOP_RE.search(isline):
                early_stop = True

            if iassign_re.search(isline) and branch_at[scan_idx] <= base_branch:
                verdict = b"assigned"
                break

            if iguarded_re.search(isline):
                continue

            if iread_re.search(isline):
                if len_guard_open >= 0 and idepth > len_guard_open:
                    continue
                verdict = b"read"
                verdict_idx = scan_idx
                break

        if verdict == b"read":
            vline = str(verdict_idx + 1).encode()
            out.append(
                b"R\t  ["
                + iname
                + b"=() iterated empty under set -u (bash 3.2 footgun)]"
            )
            out.append(
                b"R\t    " + path + b":" + str(init_idx + 1).encode() + b": " + iline
            )
            out.append(b"R\t    " + path + b":" + vline + b": " + lines[verdict_idx])
            out.append(
                b"R\t    fix: guard with 'if [ \"${#"
                + iname
                + b"[@]}\" -gt 0 ]; then ... fi'"
            )
            out.append(b"R\t         or pre-populate '" + iname + b"' before iterating")
            out.append(b"B\t" + vline + b"\t" + iname + b"\t" + path)


def main() -> int:
    paths = [p for p in sys.stdin.buffer.read().split(b"\0") if p]
    out: list[bytes] = []
    for path in paths:
        if path.endswith(EXCLUDE_SUFFIXES):
            continue
        scan_file(path, out)
    if out:
        sys.stdout.buffer.write(b"\n".join(out) + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
