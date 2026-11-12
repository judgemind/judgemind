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
# The cache filled up again in 2026-11 (#4474) after a node bump in 2026-06:
# the real cause was that nothing removed already-consumed stream entries, so
# a bigger node only delayed the OOM. The ingestion worker now trims acked
# entries every minute (framework.events.trim_consumed_entries), and every
# XADD carries a MAXLEN backstop, so memory stays bounded.
#
# Two thresholds:
#   - 80% warn: dashboard-only (no notification). It shows up on the
#     judgemind-${environment}-event-bus dashboard.
#   - 95% critical: XADDs are about to be rejected. The cause is a stopped or
#     stuck ingestion worker, and the CTA is to restart it. The alarm notifies
#     on state change, so at most once per episode.
#
# The critical alarm posts to the SNS topic the compute module owns
# (judgemind-scraper-alerts-${environment}), wired via var.alert_sns_topic_arn.
# When alerts are disabled (var.enable_alerts = false) or no topic ARN is
# supplied, the alarms are still created without an alarm action so the
# CloudWatch state-change is observable in the console even without
# notification — this preserves the diagnose-by-observation property #4470
# called for, while keeping the ARN wiring optional.

resource "aws_cloudwatch_metric_alarm" "cache_memory_warn" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-${var.environment}-cache-memory-warn"
  alarm_description = "ElastiCache (judgemind-${var.environment}) DatabaseMemoryUsagePercentage > 80% for 5 consecutive minutes. Dashboard-only (no notification). The streams are bounded, so a rise means the ingestion worker is not consuming document.captured; check the StreamLength widget on the judgemind-${var.environment}-event-bus dashboard. Do not bump node_type. See #4474."

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

  # Dashboard-only per CLAUDE.md §Human Alerts: its old CTA ("bump
  # node_type") was neither quick nor a fix, and it auto-resolves once the
  # worker catches up. The critical alarm below is the one that notifies.
  alarm_actions = []
  ok_actions    = []
}

resource "aws_cloudwatch_metric_alarm" "cache_memory_critical" {
  count = var.enable_alerts ? 1 : 0

  alarm_name        = "judgemind-${var.environment}-cache-memory-critical"
  alarm_description = "ElastiCache (judgemind-${var.environment}) DatabaseMemoryUsagePercentage > 95%: scraper XADDs to document.captured are about to be rejected, so captures stop reaching ingestion. Action: check that the judgemind-ingestion-worker-${var.environment} ECS service is running and its log shows 'Event stream trim pass'; restart it if not. Once it consumes the backlog, the trim frees memory. See #4474."

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
