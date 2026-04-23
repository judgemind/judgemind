#!/usr/bin/env python3
"""API error monitoring — CloudWatch metrics check for HTTP error rates and latency.

Queries CloudWatch ALB metrics for the API service and flags threshold breaches
for 5xx errors, 4xx spikes, and P99 latency.

This script runs in the GitHub Actions runner (not on ECS) since it only
queries CloudWatch and does not need database access.

Usage:
    python3 scripts/api-error-check.py \
        --alb-arn-suffix app/judgemind-api-dev/abc123 \
        --target-group-arn-suffix targetgroup/judgemind-api-dev/def456

Options:
    --alb-arn-suffix         ALB ARN suffix (LoadBalancer dimension).
    --target-group-arn-suffix Target group ARN suffix (TargetGroup dimension).
    --period-minutes N       Check window in minutes (default: 60).
    --5xx-threshold N        5xx count threshold (default: 10).
    --4xx-threshold N        4xx count threshold (default: 100).
    --latency-threshold N    P99 latency threshold in seconds (default: 5).
    --json                   Machine-readable JSON output (default).
    --text                   Human-readable text output.

Exit code: 0 if all healthy, 1 if alerts found.
"""
# permanent: true

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3


def get_metric_sum(
    client: Any,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    start_time: datetime,
    end_time: datetime,
    period: int,
) -> float:
    """Query CloudWatch for the sum of a metric over the given window."""
    response = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=period,
        Statistics=["Sum"],
    )
    datapoints = response.get("Datapoints", [])
    return sum(dp.get("Sum", 0) for dp in datapoints)


def get_metric_p99(
    client: Any,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    start_time: datetime,
    end_time: datetime,
    period: int,
) -> float | None:
    """Query CloudWatch for the p99 of a metric over the given window."""
    response = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=period,
        ExtendedStatistics=["p99"],
    )
    datapoints = response.get("Datapoints", [])
    if not datapoints:
        return None
    # Return the maximum p99 across all datapoints in the window
    values = [dp["ExtendedStatistics"]["p99"] for dp in datapoints if "ExtendedStatistics" in dp]
    return max(values) if values else None


def check_api_errors(
    alb_arn_suffix: str,
    tg_arn_suffix: str,
    period_minutes: int,
    threshold_5xx: int,
    threshold_4xx: int,
    threshold_latency: float,
) -> dict[str, Any]:
    """Check CloudWatch ALB metrics and return results."""
    client = boto3.client("cloudwatch", region_name="us-west-2")

    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(minutes=period_minutes)
    # Use the full period as a single datapoint
    period_seconds = period_minutes * 60

    dimensions = [
        {"Name": "LoadBalancer", "Value": alb_arn_suffix},
        {"Name": "TargetGroup", "Value": tg_arn_suffix},
    ]

    # Collect metrics
    count_5xx = get_metric_sum(
        client,
        "AWS/ApplicationELB",
        "HTTPCode_Target_5XX_Count",
        dimensions,
        start_time,
        end_time,
        period_seconds,
    )

    count_4xx = get_metric_sum(
        client,
        "AWS/ApplicationELB",
        "HTTPCode_Target_4XX_Count",
        dimensions,
        start_time,
        end_time,
        period_seconds,
    )

    count_2xx = get_metric_sum(
        client,
        "AWS/ApplicationELB",
        "HTTPCode_Target_2XX_Count",
        dimensions,
        start_time,
        end_time,
        period_seconds,
    )

    latency_p99 = get_metric_p99(
        client,
        "AWS/ApplicationELB",
        "TargetResponseTime",
        dimensions,
        start_time,
        end_time,
        period_seconds,
    )

    # Evaluate thresholds
    alerts: list[dict[str, Any]] = []

    if count_5xx >= threshold_5xx:
        alerts.append({
            "metric": "5xx_errors",
            "value": count_5xx,
            "threshold": threshold_5xx,
            "message": f"5xx error count ({int(count_5xx)}) exceeded threshold ({threshold_5xx})",
        })

    if count_4xx >= threshold_4xx:
        alerts.append({
            "metric": "4xx_errors",
            "value": count_4xx,
            "threshold": threshold_4xx,
            "message": f"4xx error count ({int(count_4xx)}) exceeded threshold ({threshold_4xx})",
        })

    if latency_p99 is not None and latency_p99 >= threshold_latency:
        alerts.append({
            "metric": "p99_latency",
            "value": round(latency_p99, 3),
            "threshold": threshold_latency,
            "message": f"P99 latency ({latency_p99:.3f}s) exceeded threshold ({threshold_latency}s)",
        })

    return {
        "timestamp": end_time.isoformat(),
        "period_minutes": period_minutes,
        "metrics": {
            "5xx_count": int(count_5xx),
            "4xx_count": int(count_4xx),
            "2xx_count": int(count_2xx),
            "p99_latency_seconds": round(latency_p99, 3) if latency_p99 is not None else None,
        },
        "alerts": alerts,
        "healthy": len(alerts) == 0,
    }


def format_text(result: dict[str, Any]) -> str:
    """Format results as human-readable text."""
    lines = [
        f"API Error Check — {result['timestamp']}",
        f"Period: last {result['period_minutes']} minutes",
        "",
        "Metrics:",
        f"  2xx responses: {result['metrics']['2xx_count']}",
        f"  4xx responses: {result['metrics']['4xx_count']}",
        f"  5xx responses: {result['metrics']['5xx_count']}",
        f"  P99 latency:   {result['metrics']['p99_latency_seconds']}s"
        if result["metrics"]["p99_latency_seconds"] is not None
        else "  P99 latency:   N/A (no traffic)",
        "",
    ]

    if result["healthy"]:
        lines.append("Status: HEALTHY — all metrics within thresholds")
    else:
        lines.append(f"Status: ALERT — {len(result['alerts'])} threshold(s) breached")
        for alert in result["alerts"]:
            lines.append(f"  - {alert['message']}")

    return "\n".join(lines)


def main() -> int:
    """Run the API error check."""
    parser = argparse.ArgumentParser(description="API error monitoring via CloudWatch metrics")
    parser.add_argument("--alb-arn-suffix", required=True, help="ALB ARN suffix (LoadBalancer dimension)")
    parser.add_argument(
        "--target-group-arn-suffix", required=True, help="Target group ARN suffix (TargetGroup dimension)"
    )
    parser.add_argument("--period-minutes", type=int, default=60, help="Check window in minutes")
    parser.add_argument("--5xx-threshold", type=int, default=10, dest="threshold_5xx", help="5xx count threshold")
    parser.add_argument("--4xx-threshold", type=int, default=100, dest="threshold_4xx", help="4xx count threshold")
    parser.add_argument(
        "--latency-threshold", type=float, default=5.0, dest="threshold_latency", help="P99 latency threshold (seconds)"
    )

    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", default=True, dest="json_output", help="JSON output")
    output_group.add_argument("--text", action="store_true", dest="text_output", help="Text output")

    args = parser.parse_args()

    result = check_api_errors(
        alb_arn_suffix=args.alb_arn_suffix,
        tg_arn_suffix=args.target_group_arn_suffix,
        period_minutes=args.period_minutes,
        threshold_5xx=args.threshold_5xx,
        threshold_4xx=args.threshold_4xx,
        threshold_latency=args.threshold_latency,
    )

    if args.text_output:
        print(format_text(result))
    else:
        print(json.dumps(result, indent=2))

    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
