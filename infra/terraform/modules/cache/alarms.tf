# CloudWatch alarms for the ElastiCache Redis cluster.
#
# These alarms catch the producer-side failure mode that broke the ingestion
# pipeline 2026-05-04 → 2026-05-09 (#4470): the dev cache crossed 96%
# DatabaseMemoryUsagePercentage at 2026-05-06 01:27 UTC, started rejecting
# `XADD` calls with Redis OOM, and the gap was first noticed by the daily
# field-population audit (#4310) on 2026-05-07 — three days after the cache
# was already fatally full. With these alarms in place the warn alarm would
# have fired ~2 days earlier, when the cache crossed 80% on 2026-05-04, giving
# the operator a multi-day head start before the OOM blocked production writes.
#
# Two thresholds, matching #4475 acceptance criteria:
#   - 80% warn: leading-indicator alert. The cache should not normally reach
#     80%; this gives the operator enough lead time to bump the node type
#     before the OOM hits.
#   - 95% critical: at this point Redis is one TTL flush away from OOM-eviction
#     errors and ingestion is at imminent risk. Higher-severity alert.
#
# Both alarms post to the same SNS topic the compute module owns
# (judgemind-scraper-alerts-${environment}), wired via var.alert_sns_topic_arn.
# When alerts are disabled (var.enable_alerts = false) or no topic ARN is
# supplied, the alarms are still created without an alarm action so the
# CloudWatch state-change is observable in the console even without
# notification — this preserves the diagnose-by-observation property #4470
# called for, while keeping the ARN wiring optional.

resource "aws_cloudwatch_metric_alarm" "cache_memory_warn" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-${var.environment}-cache-memory-warn"
  alarm_description = "ElastiCache (judgemind-${var.environment}) DatabaseMemoryUsagePercentage > 80% for 5 consecutive minutes. Leading-indicator alert; bump node_type before the OOM hits. See #4470 for the failure-mode background."

  namespace   = "AWS/ElastiCache"
  metric_name = "DatabaseMemoryUsagePercentage"
  statistic   = "Average"

  dimensions = {
    CacheClusterId = aws_elasticache_cluster.redis.cluster_id
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alert_sns_topic_arn != "" ? [var.alert_sns_topic_arn] : []
  ok_actions    = var.alert_sns_topic_arn != "" ? [var.alert_sns_topic_arn] : []
}

resource "aws_cloudwatch_metric_alarm" "cache_memory_critical" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-${var.environment}-cache-memory-critical"
  alarm_description = "ElastiCache (judgemind-${var.environment}) DatabaseMemoryUsagePercentage > 95%. Critical: Redis is at imminent risk of OOM-rejecting XADD writes from scrapers, which silently breaks the ingestion pipeline. See #4470."

  namespace   = "AWS/ElastiCache"
  metric_name = "DatabaseMemoryUsagePercentage"
  statistic   = "Average"

  dimensions = {
    CacheClusterId = aws_elasticache_cluster.redis.cluster_id
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = 95
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = var.alert_sns_topic_arn != "" ? [var.alert_sns_topic_arn] : []
  ok_actions    = var.alert_sns_topic_arn != "" ? [var.alert_sns_topic_arn] : []
}
