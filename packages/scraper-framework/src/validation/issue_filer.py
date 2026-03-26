"""Auto-file GitHub issues for validation flag/fail results.

Files GitHub issues via the REST API when the ingestion validation gate
detects data quality problems. Issues are deduplicated to avoid flooding
the backlog with identical findings.

Configuration:
- ``GITHUB_TOKEN``: GitHub personal access token (required for issue creation).
- ``GITHUB_REPO``: Repository in ``owner/repo`` format (default: ``judgemind/judgemind``).

See: #1803, architecture spec §3.5.3.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REPO = "judgemind/judgemind"
_GITHUB_API_BASE = "https://api.github.com"
_TIMEOUT = 30.0

# Labels applied to all validation issues.
_BASE_LABELS = ["type/bug", "area/scraping", "data-quality-failure"]

# Additional labels per result type.
_RESULT_LABELS: dict[str, list[str]] = {
    "flag": ["priority/p2"],
    "fail": ["priority/p1", "agent/ready"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def file_validation_issue(
    *,
    result: str,
    reason: str,
    county: str,
    case_number: str | None,
    document_id: str,
    s3_key: str | None,
    ruling_text_excerpt: str | None,
    extracted_fields: dict[str, Any] | None = None,
    github_token: str | None = None,
    github_repo: str | None = None,
    http_client: httpx.Client | None = None,
) -> str | None:
    """File a GitHub issue for a validation flag/fail result.

    Deduplicates by checking for existing open issues with the same
    ``data-quality-failure`` label and matching document_id or pattern
    (same county + same reason substring).

    Parameters
    ----------
    result : str
        Validation result: ``"flag"`` or ``"fail"``.
    reason : str
        Human-readable validation reason.
    county : str
        County name for the document.
    case_number : str | None
        Case number, if available.
    document_id : str
        The document's unique ID.
    s3_key : str | None
        S3 archive key for the document.
    ruling_text_excerpt : str | None
        First ~200 chars of ruling text for context.
    extracted_fields : dict | None
        Dictionary of extracted field values for the issue body.
    github_token : str | None
        GitHub token. Falls back to ``GITHUB_TOKEN`` env var.
    github_repo : str | None
        Repository in ``owner/repo`` format. Falls back to ``GITHUB_REPO``
        env var, then ``judgemind/judgemind``.
    http_client : httpx.Client | None
        Optional pre-created httpx client for connection reuse.

    Returns
    -------
    str | None
        The URL of the created issue, or None if skipped (duplicate) or failed.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "GITHUB_TOKEN not set — skipping issue filing for validation %s",
            result,
        )
        return None

    repo = github_repo or os.environ.get("GITHUB_REPO", _DEFAULT_REPO)

    # Build a reason summary for the title (first 60 chars).
    reason_summary = reason[:60] if reason else "unknown reason"
    case_ref = case_number or "unknown"
    title = f"fix(ingestion): validation {result} \u2014 {county} {case_ref} {reason_summary}"
    # GitHub issue titles have no formal limit, but truncate for readability.
    if len(title) > 150:
        title = title[:147] + "..."

    # Check for duplicates before creating.
    client = http_client or httpx.Client(timeout=_TIMEOUT)
    should_close = http_client is None
    try:
        if _is_duplicate(
            client=client,
            token=token,
            repo=repo,
            document_id=document_id,
            county=county,
            reason=reason,
        ):
            logger.info(
                "Skipping duplicate validation issue",
                extra={
                    "document_id": document_id,
                    "county": county,
                    "result": result,
                },
            )
            return None

        # Build issue body.
        body = _build_issue_body(
            result=result,
            reason=reason,
            county=county,
            case_number=case_number,
            document_id=document_id,
            s3_key=s3_key,
            ruling_text_excerpt=ruling_text_excerpt,
            extracted_fields=extracted_fields,
        )

        # Determine labels.
        labels = list(_BASE_LABELS)
        labels.extend(_RESULT_LABELS.get(result, []))

        # Create the issue.
        url = f"{_GITHUB_API_BASE}/repos/{repo}/issues"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {
            "title": title,
            "body": body,
            "labels": labels,
        }

        resp = client.post(url, headers=headers, json=payload)
        if resp.status_code == 201:
            issue_url = resp.json().get("html_url", "")
            logger.info(
                "Filed validation issue",
                extra={
                    "document_id": document_id,
                    "result": result,
                    "issue_url": issue_url,
                },
            )
            return issue_url
        else:
            logger.warning(
                "Failed to create GitHub issue",
                extra={
                    "status_code": resp.status_code,
                    "response": resp.text[:200],
                    "document_id": document_id,
                },
            )
            return None
    except Exception as exc:
        logger.warning(
            "Exception while filing validation issue — skipping",
            extra={"error": str(exc), "document_id": document_id},
        )
        return None
    finally:
        if should_close:
            client.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_duplicate(
    *,
    client: httpx.Client,
    token: str,
    repo: str,
    document_id: str,
    county: str,
    reason: str,
) -> bool:
    """Check if an open issue already exists for this document or pattern.

    A duplicate is detected if any open issue with ``data-quality-failure``
    label either:
    1. Contains the exact document_id in its body, OR
    2. Matches the same county AND a similar reason substring in its title.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Search for existing issues with data-quality-failure label.
    # First check by document_id (exact match).
    search_url = f"{_GITHUB_API_BASE}/search/issues"
    query = f"repo:{repo} is:issue is:open label:data-quality-failure {document_id}"
    try:
        resp = client.get(
            search_url,
            headers=headers,
            params={"q": query, "per_page": 1},
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("total_count", 0) > 0:
                return True
    except Exception as exc:
        logger.warning(
            "GitHub search failed during dedup check — proceeding with issue creation",
            extra={"error": str(exc)},
        )
        return False

    # Check by pattern: same county + similar reason in title.
    # Use a truncated reason (first 30 chars) for fuzzy title matching.
    reason_prefix = reason[:30] if reason else ""
    if reason_prefix and county:
        pattern_query = (
            f"repo:{repo} is:issue is:open label:data-quality-failure "
            f"{county} {reason_prefix} in:title"
        )
        try:
            resp = client.get(
                search_url,
                headers=headers,
                params={"q": pattern_query, "per_page": 1},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("total_count", 0) > 0:
                    return True
        except Exception as exc:
            logger.warning(
                "GitHub pattern search failed — proceeding with issue creation",
                extra={"error": str(exc)},
            )

    return False


def _build_issue_body(
    *,
    result: str,
    reason: str,
    county: str,
    case_number: str | None,
    document_id: str,
    s3_key: str | None,
    ruling_text_excerpt: str | None,
    extracted_fields: dict[str, Any] | None,
) -> str:
    """Build the GitHub issue body with validation context."""
    lines: list[str] = []
    lines.append(f"## Validation {result.upper()}")
    lines.append("")
    lines.append(f"**Reason:** {reason}")
    lines.append(f"**County:** {county}")
    if case_number:
        lines.append(f"**Case Number:** {case_number}")
    lines.append(f"**Document ID:** `{document_id}`")
    if s3_key:
        lines.append(f"**S3 Key:** `{s3_key}`")
    lines.append("")

    if extracted_fields:
        lines.append("### Extracted Fields")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        for field, value in extracted_fields.items():
            display_value = str(value) if value is not None else "(null)"
            # Escape pipe characters in values for markdown table.
            display_value = display_value.replace("|", "\\|")
            lines.append(f"| {field} | {display_value} |")
        lines.append("")

    if ruling_text_excerpt:
        lines.append("### Ruling Text Excerpt")
        lines.append("")
        lines.append("```")
        # Limit excerpt length and escape backticks.
        excerpt = ruling_text_excerpt[:300]
        excerpt = excerpt.replace("```", "` ` `")
        lines.append(excerpt)
        if len(ruling_text_excerpt) > 300:
            lines.append("[...]")
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append("*Auto-filed by ingestion validation gate (#1803)*")
    return "\n".join(lines)
