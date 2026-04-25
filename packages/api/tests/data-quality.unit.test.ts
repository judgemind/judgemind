/**
 * Unit tests for data quality health status computation.
 */

import { describe, it, expect } from 'vitest';
import { computeHealthStatus, type CountyMetrics } from '../src/graphql/data-quality';

describe('computeHealthStatus', () => {
  it('returns green when all metrics are healthy', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 50,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('returns red when no data is available', () => {
    const metrics: CountyMetrics = {
      county: 'Test',
      rulingCount24h: null,
      fieldCompletenessPct: null,
      scraperLastSuccessAgeHours: null,
      lastUpdated: null,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when scraper is down > 24h', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 30,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when field completeness is below 70%', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 60,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red when zero rulings', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 0,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns yellow when completeness is between 70-90%', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 80,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns green when scraper age is 12h (below 13h yellow threshold)', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 12,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('returns yellow when scraper age is 13h (at yellow threshold)', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 13,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns yellow when scraper age is 13-25h', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 20,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns green when some metrics are null but available ones are healthy', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: null,
      scraperLastSuccessAgeHours: null,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('returns yellow at boundary: completeness exactly 70%', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 70,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns green at boundary: completeness exactly 90%', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 90,
      scraperLastSuccessAgeHours: 2,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('returns yellow at boundary: scraper age exactly 13h', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 13,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('returns red at boundary: scraper age exactly 26h (> 25 check)', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 26,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('returns red at boundary: scraper age exactly 25h (> 25 check — 25 is not > 25 so yellow)', () => {
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 25,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    // 25 is not > 25 so it's yellow, not red
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });
});
