# CloudWatch dashboard for the Redis Streams event bus (#4474).
#
# Dashboard-only monitoring per CLAUDE.md §Human Alerts. It shows whether
# cache memory stays bounded and whether the ingestion worker keeps up:
#   - DatabaseMemoryUsagePercentage, with the 80% warn and 95% critical
#     alarm thresholds drawn on it (alarms.tf).
#   - Judgemind/EventBus StreamLength / EntriesTrimmed: log-derived metrics
#     the compute module extracts from the ingestion worker's once-a-minute
#     "Event stream trim pass" log line. After trimming, StreamLength is the
#     unconsumed backlog. If it rises while memory rises, the worker is
#     behind or down.
#   - Evictions and CurrItems for context.

resource "aws_cloudwatch_dashboard" "event_bus" {
  dashboard_name = "judgemind-${var.environment}-event-bus"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Redis memory % (judgemind-${var.environment})"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/ElastiCache", "DatabaseMemoryUsagePercentage", "CacheClusterId", aws_elasticache_cluster.redis.cluster_id],
          ]
          yAxis = { left = { min = 0, max = 100 } }
          annotations = {
            horizontal = [
              { label = "warn (dashboard-only)", value = 80 },
              { label = "critical", value = 95 },
            ]
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "document.captured length after trim (unconsumed backlog)"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Maximum"
          period = 300
          metrics = [
            ["Judgemind/EventBus", "StreamLength"],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Acknowledged entries trimmed"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["Judgemind/EventBus", "EntriesTrimmed"],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Redis keys and evictions"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/ElastiCache", "CurrItems", "CacheClusterId", aws_elasticache_cluster.redis.cluster_id],
            ["AWS/ElastiCache", "Evictions", "CacheClusterId", aws_elasticache_cluster.redis.cluster_id, { stat = "Sum", yAxis = "right" }],
          ]
        }
      },
    ]
  })
}

data "aws_region" "current" {}
