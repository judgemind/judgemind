#!/usr/bin/env bash
# test_check_parse_document_reingest_safety.sh — Tests for
# check-parse-document-reingest-safety.sh (issue #4141).
#
# Creates temporary Python files that exercise the classifier's
# pass-and-fail cases — Live-only with marker (passes), Live-only
# without marker (fails), Reingest-aware (passes), Mixed (passes).
#
# Usage:
#   scripts/tests/test_check_parse_document_reingest_safety.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-parse-document-reingest-safety.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: Live-only with "Reingest hazard" marker passes ──────────────
create_test_file "live_only_marker_hazard.py" 'class S:
    def parse_document(self, doc):
        """No-op — fields populated during fetch.

        Reingest hazard (audit #4046): on reingest, judge_name and
        department are cleared by the merge logic.  See follow-up #4133.
        """
        return doc
' > /dev/null
assert_passes "Live-only with 'Reingest hazard' marker passes"
reset_tmpdir

# ─── Test 2: Live-only with "no-op on the reingest path" marker passes ───
create_test_file "live_only_marker_oc.py" 'class S:
    def parse_document(self, doc):
        """No-op: field extraction is handled by the multimodal LLM pipeline.

        This is also a no-op on the reingest path
        (scripts/reingest_from_s3.py).  See audit #4046.
        """
        return doc
' > /dev/null
assert_passes "Live-only with 'no-op on the reingest path' marker passes"
reset_tmpdir

# ─── Test 3: Live-only with "not reingest-safe" marker passes ────────────
create_test_file "live_only_marker_unsafe.py" 'class S:
    def parse_document(self, doc):
        """No-op — this scraper is not reingest-safe (see #4141)."""
        return doc
' > /dev/null
assert_passes "Live-only with 'not reingest-safe' marker passes"
reset_tmpdir

# ─── Test 4: Live-only with NO marker fails ──────────────────────────────
create_test_file "live_only_no_marker.py" 'class S:
    def parse_document(self, doc):
        """No-op — fields populated during fetch."""
        return doc
' > /dev/null
assert_fails "Live-only without marker fails"
reset_tmpdir

# ─── Test 5: Live-only with NO docstring at all fails ────────────────────
create_test_file "live_only_no_docstring.py" 'class S:
    def parse_document(self, doc):
        return doc
' > /dev/null
assert_fails "Live-only without a docstring fails"
reset_tmpdir

# ─── Test 6: Reingest-aware (reads doc.raw_content) passes ───────────────
# No marker required — only Live-only methods need the marker.
create_test_file "reingest_aware_raw_content.py" 'class S:
    def parse_document(self, doc):
        """Parse PDF text from raw_content."""
        text = _extract_pdf_text(doc.raw_content)
        doc.ruling_text = text
        return doc
' > /dev/null
assert_passes "Reingest-aware (reads doc.raw_content) passes without marker"
reset_tmpdir

# ─── Test 7: Reingest-aware via super().parse_document(doc) passes ───────
create_test_file "reingest_aware_super.py" 'class S(Base):
    def parse_document(self, doc):
        """Extend base parse with court-specific fallbacks."""
        doc = super().parse_document(doc)
        if doc.ruling_text and not doc.judge_name:
            doc.judge_name = _extract_judge(doc.ruling_text)
        return doc
' > /dev/null
assert_passes "Reingest-aware via super().parse_document(doc) passes without marker"
reset_tmpdir

# ─── Test 8: Reingest-aware via parser.parse_document(doc) passes ────────
# Mirrors sd_pipeline.py — delegate to a stored instance of another scraper.
create_test_file "reingest_aware_delegate_instance.py" 'class S:
    def parse_document(self, doc):
        """Delegate to Phase 2 parser."""
        parser = self._get_phase2_parser()
        return parser.parse_document(doc)
' > /dev/null
assert_passes "Reingest-aware via instance.parse_document(doc) passes"
reset_tmpdir

# ─── Test 9: Mixed (branches on pre_split, else reads raw_content) ───────
create_test_file "mixed_pre_split.py" 'class S:
    def parse_document(self, doc):
        """Mixed — short-circuit on pre_split, else parse PDF text."""
        if doc.extra.get("pre_split") or doc.extra.get("_llm_extracted"):
            return doc
        text = _extract_pdf_text(doc.raw_content)
        doc.ruling_text = text
        return doc
' > /dev/null
assert_passes "Mixed (pre_split short-circuit + raw_content read) passes"
reset_tmpdir

# ─── Test 10: self.parse_document recursion is NOT delegation ────────────
# A method that only calls self.parse_document(...) is recursing, not
# delegating to a different parser — so it should still be Live-only
# and the marker is required.
create_test_file "self_recursion.py" 'class S:
    def parse_document(self, doc):
        """Self-recursion only — should still require marker."""
        if doc.extra.get("retry"):
            return self.parse_document(doc)
        return doc
' > /dev/null
assert_fails "self.parse_document recursion does not count as delegation"
reset_tmpdir

# ─── Test 11: Module-level parse_document function is ignored ────────────
# The audit scope is class methods on scraper subclasses.  A
# module-level function named parse_document is not a scraper method
# and should not be classified.
create_test_file "module_level.py" 'def parse_document(doc):
    """Module-level helper, not a scraper method."""
    return doc
' > /dev/null
assert_passes "Module-level parse_document function is ignored (not a class method)"
reset_tmpdir

# ─── Test 12: Method named something else is ignored ─────────────────────
create_test_file "other_method.py" 'class S:
    def parse_doc(self, doc):
        """Different method name — not in scope."""
        return doc
' > /dev/null
assert_passes "Method with a different name is ignored"
reset_tmpdir

# ─── Test 13: Marker matching is case-insensitive ────────────────────────
create_test_file "case_insensitive.py" 'class S:
    def parse_document(self, doc):
        """REINGEST HAZARD — uppercase variant of the marker."""
        return doc
' > /dev/null
assert_passes "Marker matching is case-insensitive (uppercase)"
reset_tmpdir

# ─── Test 14: Empty file passes ──────────────────────────────────────────
create_test_file "empty.py" '"""Empty module."""
' > /dev/null
assert_passes "Empty module passes"
reset_tmpdir

# ─── Test 15: Syntactically broken Python is skipped silently ────────────
create_test_file "broken.py" 'class S(:  # syntax error
    pass
' > /dev/null
assert_passes "Syntactically broken Python is skipped silently"
reset_tmpdir

# ─── Test 16: Direct production-tree run reports zero violations ─────────
# Defense-in-depth — the production tree (the default scan root) is
# clean today (after #4046 landed all three Live-only docstring
# updates).  Removing the marker from any of the three known Live-only
# scrapers should fail the check, but we don't test that here because
# it would require mutating the production tree in CI.  The fixture
# tests (1-15) cover the equivalent behavior on synthetic files.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Production courts/ tree passes the check"
else
    echo "FAIL: Production courts/ tree fails the check (expected pass)"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 17: AST scan ignores .venv/ subdir ─────────────────────────────
mkdir -p "$TMPDIR_TEST/.venv/lib/python3.12/site-packages"
printf 'class V:\n    def parse_document(self, doc):\n        """No marker — but vendored, should be ignored."""\n        return doc\n' \
    > "$TMPDIR_TEST/.venv/lib/python3.12/site-packages/vendored.py"
assert_passes ".venv/ subdir is excluded from the scan"
reset_tmpdir

# ─── Test 18: Multiple Live-only methods in one file are all reported ────
create_test_file "two_violations.py" 'class A:
    def parse_document(self, doc):
        """No marker."""
        return doc

class B:
    def parse_document(self, doc):
        """Also no marker."""
        return doc
' > /dev/null
assert_fails "Multiple Live-only violations in one file are caught"
reset_tmpdir

# ─── Test 19: Live-only with marker + Live-only without — fails ──────────
# Mixed-state test: one good, one bad in the same scan tree.  The
# scanner must still fail because at least one violator exists.
create_test_file "good_and_bad/good.py" 'class Good:
    def parse_document(self, doc):
        """Reingest hazard — see audit #4046."""
        return doc
' > /dev/null
create_test_file "good_and_bad/bad.py" 'class Bad:
    def parse_document(self, doc):
        """No marker."""
        return doc
' > /dev/null
assert_fails "Mixed pass+fail tree fails because of the bad file"
reset_tmpdir

# ─── Test 20: No self-match on ci.yml step name ──────────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-parse-document-reingest-safety.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
