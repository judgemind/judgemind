#!/usr/bin/env python3
"""Hourly site quality check — validates page content and GraphQL API responses.

Reads baselines from site-quality-baselines.json and checks:
  1. Page rendering: HTTP 200 + expected content markers present
  2. GraphQL queries: non-empty responses for key queries
  3. Functional checks: search returns results, detail pages have expected fields

Outputs GitHub Actions step outputs for downstream issue filing.
Exits 0 if all checks pass, 1 if any fail.

Environment variables:
  BASE_URL       - Frontend URL (default: https://dev.judgemind.org)
  API_URL        - GraphQL endpoint (default: https://dev.api.judgemind.org/graphql)
  BASELINES_FILE - Path to baselines JSON (default: site-quality-baselines.json)
  GITHUB_OUTPUT  - GitHub Actions output file (optional, for CI integration)
"""
# permanent: true

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckResult:
    """Result of a single quality check."""

    name: str
    category: str  # "page", "graphql", "functional"
    passed: bool
    details: str = ""


@dataclass
class QualityReport:
    """Aggregated results from all quality checks."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    @property
    def all_passed(self) -> bool:
        return len(self.failures) == 0


def http_get(url: str, timeout: int = 30) -> tuple[int, str]:
    """Fetch a URL and return (status_code, body). Returns (0, error) on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JudgemindQualityCheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, str(e)


def graphql_query(api_url: str, query: str, timeout: int = 15) -> dict[str, Any]:
    """Execute a GraphQL query. Returns the parsed JSON response or an error dict."""
    payload = json.dumps({"query": query}).encode("utf-8")
    try:
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "JudgemindQualityCheck/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def resolve_path(data: Any, path: str) -> Any:
    """Resolve a dot-separated path into a nested dict/list structure.

    Supports integer indices for lists, e.g. "data.edges.0.node.id".
    Returns None if the path cannot be resolved.
    """
    parts = path.split(".")
    current = data
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def check_pages(
    base_url: str, pages: list[dict[str, Any]]
) -> list[CheckResult]:
    """Check that each page returns 200 and contains expected content markers."""
    results = []
    for page in pages:
        name = page["name"]
        path = page["path"]
        markers = page.get("content_markers", [])
        url = f"{base_url}{path}"

        status, body = http_get(url)

        if status != 200:
            results.append(CheckResult(
                name=name,
                category="page",
                passed=False,
                details=f"HTTP {status} for {path}",
            ))
            continue

        missing = [m for m in markers if m.lower() not in body.lower()]
        if missing:
            results.append(CheckResult(
                name=name,
                category="page",
                passed=False,
                details=f"Missing content markers on {path}: {', '.join(missing)}",
            ))
        else:
            results.append(CheckResult(
                name=name,
                category="page",
                passed=True,
                details=f"{path} OK ({len(markers)} markers found)",
            ))

    return results


def check_graphql_queries(
    api_url: str, queries: list[dict[str, Any]]
) -> list[CheckResult]:
    """Validate GraphQL queries return expected data."""
    results = []
    for q in queries:
        name = q["name"]
        query = q["query"]
        expected_path = q["expected_path"]

        resp = graphql_query(api_url, query)

        if "error" in resp and not isinstance(resp.get("data"), dict):
            results.append(CheckResult(
                name=name,
                category="graphql",
                passed=False,
                details=f"Query failed: {resp['error']}",
            ))
            continue

        if "errors" in resp:
            error_msg = resp["errors"][0].get("message", "Unknown error") if resp["errors"] else "Unknown"
            results.append(CheckResult(
                name=name,
                category="graphql",
                passed=False,
                details=f"GraphQL error: {error_msg}",
            ))
            continue

        value = resolve_path(resp, expected_path)

        # Check expected_value (exact match)
        if "expected_value" in q:
            if value == q["expected_value"]:
                results.append(CheckResult(
                    name=name, category="graphql", passed=True,
                    details=f"Value matches: {value}",
                ))
            else:
                results.append(CheckResult(
                    name=name, category="graphql", passed=False,
                    details=f"Expected '{q['expected_value']}' at {expected_path}, got '{value}'",
                ))
            continue

        # Check min_items (list length)
        if "min_items" in q:
            if isinstance(value, list) and len(value) >= q["min_items"]:
                results.append(CheckResult(
                    name=name, category="graphql", passed=True,
                    details=f"{len(value)} items returned (min {q['min_items']})",
                ))
            else:
                count = len(value) if isinstance(value, list) else 0
                results.append(CheckResult(
                    name=name, category="graphql", passed=False,
                    details=f"Expected >= {q['min_items']} items at {expected_path}, got {count}",
                ))
            continue

        # Default: just check it's not None/empty
        if value:
            results.append(CheckResult(
                name=name, category="graphql", passed=True,
                details=f"Value present at {expected_path}",
            ))
        else:
            results.append(CheckResult(
                name=name, category="graphql", passed=False,
                details=f"Empty or missing value at {expected_path}",
            ))

    return results


def check_functional(
    api_url: str, functional: dict[str, Any]
) -> list[CheckResult]:
    """Run functional checks: search, judge profile, ruling detail."""
    results = []

    # Search check
    if "search" in functional:
        search = functional["search"]
        resp = graphql_query(api_url, search["query"])
        value = resolve_path(resp, search["expected_path"])
        if isinstance(value, (int, float)) and value >= search.get("min_value", 1):
            results.append(CheckResult(
                name="Search functionality",
                category="functional",
                passed=True,
                details=f"Search returned {value} hits",
            ))
        else:
            results.append(CheckResult(
                name="Search functionality",
                category="functional",
                passed=False,
                details=f"Search returned {value} hits (expected >= {search.get('min_value', 1)})",
            ))

    # Detail page checks (judge_profile, ruling_detail)
    for check_name, check_config in functional.items():
        if check_name == "search":
            continue

        display_name = check_name.replace("_", " ").title()

        # First, get a sample ID
        sample_resp = graphql_query(api_url, check_config["sample_query"])
        sample_id = resolve_path(sample_resp, check_config["sample_id_path"])

        if not sample_id:
            results.append(CheckResult(
                name=display_name,
                category="functional",
                passed=False,
                details=f"Could not fetch sample ID from {check_config['sample_id_path']}",
            ))
            continue

        # Build and run the detail query
        template = check_config["detail_query_template"]
        # Count %s occurrences and fill them all with the sample_id
        placeholder_count = template.count("%s")
        detail_query = template % tuple([sample_id] * placeholder_count)

        detail_resp = graphql_query(api_url, detail_query)

        if "errors" in detail_resp:
            error_msg = detail_resp["errors"][0].get("message", "Unknown") if detail_resp["errors"] else "Unknown"
            results.append(CheckResult(
                name=display_name,
                category="functional",
                passed=False,
                details=f"Detail query error: {error_msg}",
            ))
            continue

        # Check all expected fields are present
        missing_fields = []
        for field_path in check_config.get("expected_fields", []):
            val = resolve_path(detail_resp, field_path)
            if val is None or val == "":
                missing_fields.append(field_path)

        if missing_fields:
            results.append(CheckResult(
                name=display_name,
                category="functional",
                passed=False,
                details=f"Missing fields for ID {sample_id}: {', '.join(missing_fields)}",
            ))
        else:
            field_count = len(check_config.get("expected_fields", []))
            results.append(CheckResult(
                name=display_name,
                category="functional",
                passed=True,
                details=f"ID {sample_id}: all {field_count} expected fields present",
            ))

    return results


def write_github_output(report: QualityReport) -> None:
    """Write GitHub Actions step outputs."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return

    with open(output_file, "a") as f:
        if report.all_passed:
            f.write("has_failures=false\n")
        else:
            f.write("has_failures=true\n")
            # Build failure details as pipe-delimited table rows
            rows = []
            for failure in report.failures:
                # Escape pipe characters in details
                safe_details = failure.details.replace("|", "\\|")
                rows.append(f"| {failure.name} | {failure.category} | {safe_details} |")
            details = "\n".join(rows)
            # Use heredoc-style multiline output
            f.write("failure_details<<GHEOF\n")
            f.write(details + "\n")
            f.write("GHEOF\n")


def main() -> int:
    """Run all quality checks and report results."""
    base_url = os.environ.get("BASE_URL", "https://dev.judgemind.org")
    api_url = os.environ.get("API_URL", "https://dev.api.judgemind.org/graphql")
    baselines_file = os.environ.get("BASELINES_FILE", "site-quality-baselines.json")

    # Load baselines
    try:
        with open(baselines_file) as f:
            baselines = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: Baselines file not found: {baselines_file}")
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in baselines file: {e}")
        return 1

    report = QualityReport()

    # 1. Page rendering checks
    print("=== Page Rendering Checks ===")
    page_results = check_pages(base_url, baselines.get("pages", []))
    report.results.extend(page_results)
    for r in page_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {status}: {r.name} — {r.details}")

    # 2. GraphQL query checks
    print("\n=== GraphQL Query Checks ===")
    gql_results = check_graphql_queries(api_url, baselines.get("graphql_queries", []))
    report.results.extend(gql_results)
    for r in gql_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {status}: {r.name} — {r.details}")

    # 3. Functional checks
    print("\n=== Functional Checks ===")
    func_results = check_functional(api_url, baselines.get("functional_checks", {}))
    report.results.extend(func_results)
    for r in func_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {status}: {r.name} — {r.details}")

    # Summary
    total = len(report.results)
    passed = sum(1 for r in report.results if r.passed)
    failed = total - passed
    print(f"\n=== Summary: {passed}/{total} checks passed, {failed} failed ===")

    # Write GitHub Actions outputs
    write_github_output(report)

    if not report.all_passed:
        print("\nFailing checks:")
        for f in report.failures:
            print(f"  - [{f.category}] {f.name}: {f.details}")
        return 1

    print("\nAll quality checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
