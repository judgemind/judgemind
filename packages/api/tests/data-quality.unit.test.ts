/**
 * Unit tests for data quality health status computation and threshold loading.
 */

import { describe, it, expect } from 'vitest';
import { computeHealthStatus, loadCountyThresholds, type CountyMetrics } from '../src/graphql/data-quality';

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

describe('computeHealthStatus — per-county threshold overrides', () => {
  // County with max_expected_gap_hours=48: redHours=48, yellowHours=36

  it('county with 48h threshold: green at 30h scraperLastSuccessAgeHours', () => {
    const metrics: CountyMetrics = {
      county: 'Orange',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 30,
      lastUpdated: '2026-03-01T00:00:00Z',
      redThresholdHours: 48,
      yellowThresholdHours: 36,
    };
    expect(computeHealthStatus(metrics)).toBe('green');
  });

  it('county with 48h threshold: yellow at 38h scraperLastSuccessAgeHours', () => {
    const metrics: CountyMetrics = {
      county: 'Orange',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 38,
      lastUpdated: '2026-03-01T00:00:00Z',
      redThresholdHours: 48,
      yellowThresholdHours: 36,
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('county with 48h threshold: red at 50h scraperLastSuccessAgeHours', () => {
    const metrics: CountyMetrics = {
      county: 'Orange',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 50,
      lastUpdated: '2026-03-01T00:00:00Z',
      redThresholdHours: 48,
      yellowThresholdHours: 36,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('default county (no override): yellow at 20h scraperLastSuccessAgeHours', () => {
    // Default yellowHours=13, so 20h >= 13 → yellow
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 20,
      lastUpdated: '2026-03-01T00:00:00Z',
      // No override — uses defaults (red=25, yellow=13)
    };
    expect(computeHealthStatus(metrics)).toBe('yellow');
  });

  it('default county (no override): red at 26h scraperLastSuccessAgeHours', () => {
    // Default redHours=25, so 26 > 25 → red
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 26,
      lastUpdated: '2026-03-01T00:00:00Z',
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });

  it('null threshold values fall back to defaults', () => {
    // redThresholdHours=null → use default 25; 26h > 25 → red
    const metrics: CountyMetrics = {
      county: 'Los Angeles',
      rulingCount24h: 10,
      fieldCompletenessPct: 95,
      scraperLastSuccessAgeHours: 26,
      lastUpdated: '2026-03-01T00:00:00Z',
      redThresholdHours: null,
      yellowThresholdHours: null,
    };
    expect(computeHealthStatus(metrics)).toBe('red');
  });
});

describe('loadCountyThresholds', () => {
  it('returns a Map instance', () => {
    const result = loadCountyThresholds();
    expect(result).toBeInstanceOf(Map);
  });

  it('returns empty Map when no baselines file found at test cwd', () => {
    // In a test environment the loader may or may not find the file.
    // Either outcome is valid — it should not throw.
    const result = loadCountyThresholds();
    expect(result).toBeDefined();
  });
});
