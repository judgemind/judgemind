import { gql } from '@apollo/client';

export const DATA_QUALITY_OVERVIEW_QUERY = gql`
  query DataQualityOverview {
    dataQualityOverview {
      county
      healthStatus
      rulingCount24h
      fieldCompletenessPct
      scraperLastSuccessAgeHours
      lastUpdated
    }
  }
`;

export const DATA_QUALITY_METRICS_QUERY = gql`
  query DataQualityMetrics(
    $county: String
    $metricName: String
    $startDate: String!
    $endDate: String!
    $resolution: MetricResolution
    $first: Int
    $after: String
  ) {
    dataQualityMetrics(
      county: $county
      metricName: $metricName
      startDate: $startDate
      endDate: $endDate
      resolution: $resolution
      first: $first
      after: $after
    ) {
      edges {
        cursor
        node {
          id
          recordedAt
          county
          metricName
          metricValue
          metadata
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
`;

export const ROSTER_FRESHNESS_QUERY = gql`
  query RosterFreshness {
    rosterFreshness {
      courtId
      status
      isStale
      lastSuccessAt
      lastSnapshotAt
      lastAttemptAt
      lastOutcome
      lastErrorClass
      lastHttpStatus
      lastErrorMessage
      lastDepartmentCount
      consecutiveFailures
      ageHours
      staleThresholdHours
    }
  }
`;

export interface RosterFreshnessItem {
  courtId: string;
  status: 'ok' | 'failing' | 'stale';
  isStale: boolean;
  lastSuccessAt: string | null;
  lastSnapshotAt: string | null;
  lastAttemptAt: string | null;
  lastOutcome: 'success' | 'failure' | null;
  lastErrorClass: string | null;
  lastHttpStatus: number | null;
  lastErrorMessage: string | null;
  lastDepartmentCount: number | null;
  consecutiveFailures: number;
  ageHours: number | null;
  staleThresholdHours: number;
}

export interface RosterFreshnessData {
  rosterFreshness: RosterFreshnessItem[];
}

export interface DataQualityOverviewItem {
  county: string;
  healthStatus: 'green' | 'yellow' | 'red';
  rulingCount24h: number | null;
  fieldCompletenessPct: number | null;
  scraperLastSuccessAgeHours: number | null;
  lastUpdated: string | null;
}

export interface DataQualityOverviewData {
  dataQualityOverview: DataQualityOverviewItem[];
}

export interface DataQualityMetricNode {
  id: string;
  recordedAt: string;
  county: string;
  metricName: string;
  metricValue: number;
  metadata: string | null;
}

export interface DataQualityMetricsData {
  dataQualityMetrics: {
    edges: Array<{ cursor: string; node: DataQualityMetricNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}
