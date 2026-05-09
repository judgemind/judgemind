#!/usr/bin/env python3
# _check_shipped_pr_classify_issue.py — Classify an issue's title+body for
# audit/investigation/refactor keywords for scripts/check-shipped-pr.sh.
#
# Reads `gh issue view --json body,title,createdAt` JSON on stdin (other
# fields ignored). Prints either "audit" or "" (empty) to stdout based
# on whether the issue's title or body contains any audit-class keyword.
#
# venv: none
# permanent: true
#
# Why this classifier exists (issue #4223):
#   The `check-shipped-pr.sh` script's threshold (≥1 added overlap OR ≥2
#   total target-context overlaps) is calibrated for the canonical zombie
#   shape: a placeholder-titled PR added the file the issue's AC asks for,
#   the issue stayed `agent/ready` because no `Closes #N` keyword fired the
#   auto-close. When the issue's intent is *audit / investigation / refactor*
#   on an *already-existing* file, the same threshold misfires: a prior PR
#   created or modified the file, file overlap fires, the script reports
#   `shipped:` even though the audit work is legitimately distinct.
#
#   The date-ordering guard (#4353) closes the largest residual class —
#   "PR merged before the issue was filed". This classifier closes the
#   remaining residual: "issue created after the PR, but the PR's work is
#   not the audit work the issue is asking for". When audit-class is
#   detected, the overlap helper applies a stricter threshold: require ≥2
#   target-context overlaps AND every overlap must be ADDED. This treats
#   audit/investigation/refactor issues as inherently lower-confidence
#   shipped candidates — they almost always require new follow-up work,
#   and a single modified-file overlap is too weak a signal.
#
# Keyword list:
#   - audit, investigate, investigation
#   - refactor, refactoring
#   - migrate, migration
#   - extend, extension
#   - tighten, harden
#   - additional
#
#   Word boundaries enforced via ``\b`` to avoid false-firing on
#   substrings ("addition" vs "additional", "investor" vs "investigate").
#   "more", "still", "further" are intentionally OMITTED — too common in
#   ordinary prose ("more than", "still in flight", "for further details")
#   and would over-tighten the threshold on legitimate non-audit issues.
#
#   The classifier matches on title OR body, case-insensitively, anywhere
#   in the text. A single hit is sufficient — audit issues commonly use
#   only one of these verbs, and demanding multiple keywords would raise
#   the false-negative rate without meaningfully reducing false-positives.
#
# Exit codes:
#   0 — Always. The classifier degrades gracefully: a missing or malformed
#       JSON input is treated as "not audit-class" (empty stdout). No
#       exit-1 path because the caller treats every recoverable input as
#       "preserve pre-classifier behavior" (no extra tightening).

import json
import re
import sys

# Audit-class keywords. Word boundaries (``\b``) avoid substring matches.
# Case-insensitive at search time. The regex is anchored only by ``\b``
# on each end so it matches keywords appearing anywhere in title+body.
AUDIT_KEYWORDS_REGEX = re.compile(
    r"\b("
    r"audit|"
    r"investigate|investigation|"
    r"refactor|refactoring|"
    r"migrate|migration|"
    r"extend|extension|"
    r"tighten|"
    r"harden|"
    r"additional"
    r")\b",
    re.IGNORECASE,
)


def classify(title: str, body: str) -> bool:
    """Return True if the issue is audit-class (any keyword hit on title or body).

    The test is a single search against the concatenation. The classifier
    intentionally treats title and body equally — many audit issues
    declare their nature in the title (`investigate: audit X`) and others
    only in the body (`## Investigation task`).
    """
    combined = f"{title}\n{body}"
    return bool(AUDIT_KEYWORDS_REGEX.search(combined))


# Predicate alias (#4523). The lineage probe consumes audit-class as a
# boolean predicate ("should the lineage match be suppressed?"), and the
# semantic predicate name reads more naturally at the call site than
# ``classify(title, body)`` — which sounds like it returns the class
# label rather than a boolean. Both names point at the same regex; the
# alias is purely an ergonomics / readability improvement, not a
# behavior change.
def is_audit_class(title: str, body: str) -> bool:
    """Predicate alias of ``classify`` — True iff title+body is audit-class.

    Used by the retrospective-lineage probe (#4523) to gate FP-suppression
    on extension-class issues. The audit-class verb list (``audit``,
    ``investigate``, ``refactor``, ``migrate``, ``extend``, ``tighten``,
    ``harden``, ``additional`` plus noun forms) already covers the
    "extension-class" intent the lineage gate cares about — an issue
    titled ``extend the lineage probe`` matches via ``extend``, an issue
    titled ``additional patterns`` matches via ``additional``. Reusing
    the existing classifier keeps the verb list in one place.
    """
    return classify(title, body)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed JSON — fail open (not audit-class). Preserves
        # pre-classifier behavior, matching the pattern used by
        # _check_shipped_pr_extract_created_at.py.
        return 0

    title = data.get("title") or ""
    body = data.get("body") or ""
    if classify(title, body):
        print("audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
