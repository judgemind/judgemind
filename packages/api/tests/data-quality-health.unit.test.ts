/**
 * Unit tests for the data quality health status computation.
 * No database required — tests pure business logic.
 */

import { describe, it, expect } from 'vitest';
import { computeHealthStatus } from '../src/graphql/data-quality-resolvers';
import type { LatestMetrics } from '../src/graphql/data-quality-resolvers';

describe('computeHealthStatus', () => {
  // Green conditions
  it('returns green when all metrics are healthy', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 42,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 1.5,
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('returns green when no metrics are available (optimistic default)', () => {
    expect(computeHealthStatus({})).toBe('green');
  });

  it('returns green at exact green thresholds', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 1,
      field_completeness_pct: 90,
      scraper_last_success_age_hours: 5.99,
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  // Yellow conditions
  it('returns yellow when scraper age is between 6 and 24 hours', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 12,
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns yellow when field completeness is between 70 and 90', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 75,
      scraper_last_success_age_hours: 1,
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns yellow at exact yellow threshold (scraper age = 6)', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 6,
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns yellow at exact yellow threshold (completeness = 70)', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 70,
      scraper_last_success_age_hours: 1,
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  // Red conditions
  it('returns red when scraper is down more than 24 hours', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 25,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when field completeness is below 70', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 60,
      scraper_last_success_age_hours: 1,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when ruling count is zero', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 0,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 1,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when scraper age is exactly 24.01 (just over threshold)', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 24.01,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  // Priority: red conditions take precedence over yellow
  it('returns red even if some metrics are yellow-level', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 0, // red
      field_completeness_pct: 75, // yellow
      scraper_last_success_age_hours: 8, // yellow
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  // Partial metrics (some undefined)
  it('returns green when only ruling_count_24h is defined and > 0', () => {
    expect(computeHealthStatus({ ruling_count_24h: 5 })).toBe('green');
  });

  it('returns red when only ruling_count_24h is 0', () => {
    expect(computeHealthStatus({ ruling_count_24h: 0 })).toBe('red');
  });

  it('returns yellow when only scraper age is 12h', () => {
    expect(computeHealthStatus({ scraper_last_success_age_hours: 12 })).toBe('yellow');
  });

  // Edge case: scraper age exactly at 24h boundary
  it('returns yellow when scraper age is exactly 24 hours', () => {
    const metrics: LatestMetrics = {
      ruling_count_24h: 10,
      field_completeness_pct: 95,
      scraper_last_success_age_hours: 24,
    };
    // 24 is > 24? No. Our threshold is > 24 for red, >= 6 for yellow
    // So 24 is yellow (>= 6 but not > 24)
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });
});
