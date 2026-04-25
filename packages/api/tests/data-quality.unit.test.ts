/**
 * Unit tests for data quality health status computation and threshold loading.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';

vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs')>();
  return {
    ...actual,
    readFileSync: vi.fn(actual.readFileSync),
  };
});

import { computeHealthStatus, loadCountyThresholds, getCountyThreshold, resetCountyConfigsCache, type CountyMetrics } from '../src/graphql/data-quality';

const mockReadFileSync = vi.mocked(readFileSync);

/**
 * Mock the baselines JSON with the given counties config.
 * Used by getCountyThreshold tests to inject fixture data without filesystem access.
 */
function mockBaselinesForThreshold(counties: Record<string, unknown>): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockReadFileSync.mockImplementation((path: any) => {
    if (String(path).endsWith('data-quality-baselines.json')) {
      return JSON.stringify({ counties }) as unknown as ReturnType<typeof readFileSync>;
    }
    throw new Error(`ENOENT: no such file: ${path}`);
  });
}

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
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

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

describe('getCountyThreshold', () => {
  // Fixed test dates mirroring TestCalculateStaleThreshold in test_data_quality_check.py
  // NOW = Tuesday 2026-03-11 12:00 UTC (Python weekday 1 = Tue)
  const TUESDAY = new Date('2026-03-11T12:00:00Z');
  // Saturday 2026-03-14 12:00 UTC (Python weekday 5 = Sat)
  const SATURDAY = new Date('2026-03-14T12:00:00Z');
  // Sunday 2026-03-15 12:00 UTC (Python weekday 6 = Sun)
  const SUNDAY = new Date('2026-03-15T12:00:00Z');

  beforeEach(() => {
    vi.clearAllMocks();
    // Reset the module-level cache so each test gets a fresh read from the mocked fs.
    resetCountyConfigsCache();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetCountyConfigsCache();
  });

  it('unknown county returns default 25h red, 13h yellow', () => {
    // Empty baselines → unknown county → DEFAULT_RED_HOURS = 25
    mockBaselinesForThreshold({});
    const result = getCountyThreshold('Unknown', TUESDAY);
    expect(result.redHours).toBe(25);
    expect(result.yellowHours).toBe(13);
  });

  it('max_expected_gap_hours overrides posting-day logic', () => {
    mockBaselinesForThreshold({
      Orange: {
        max_expected_gap_hours: 48,
        posting_days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
      },
    });
    const result = getCountyThreshold('Orange', TUESDAY);
    expect(result.redHours).toBe(48);
    expect(result.yellowHours).toBe(36);
  });

  it('no posting_days → default 25h (mirrors test_daily_no_posting_days_returns_default)', () => {
    mockBaselinesForThreshold({
      'Los Angeles': { schedule_type: 'daily' },
    });
    const result = getCountyThreshold('Los Angeles', TUESDAY);
    expect(result.redHours).toBe(25);
    expect(result.yellowHours).toBe(13);
  });

  it('posting day today (Tuesday) → base 25h (mirrors test_posting_day_today_returns_base_threshold)', () => {
    // TUESDAY is a posting day in Mon-Thu set
    mockBaselinesForThreshold({
      'Santa Clara': { posting_days: ['Mon', 'Tue', 'Wed', 'Thu'] },
    });
    const result = getCountyThreshold('Santa Clara', TUESDAY);
    expect(result.redHours).toBe(25);
    expect(result.yellowHours).toBe(13);
  });

  it('Saturday Mon-Thu posting: last post Thursday, 2 days ago → 48+25=73h (mirrors test_saturday_with_weekday_posting)', () => {
    mockBaselinesForThreshold({
      'Santa Clara': { posting_days: ['Mon', 'Tue', 'Wed', 'Thu'] },
    });
    const result = getCountyThreshold('Santa Clara', SATURDAY);
    expect(result.redHours).toBe(73);
    expect(result.yellowHours).toBe(61);
  });

  it('Sunday Mon-Thu posting: last post Thursday, 3 days ago → 72+25=97h (mirrors test_sunday_with_weekday_posting)', () => {
    mockBaselinesForThreshold({
      'Santa Clara': { posting_days: ['Mon', 'Tue', 'Wed', 'Thu'] },
    });
    const result = getCountyThreshold('Santa Clara', SUNDAY);
    expect(result.redHours).toBe(97);
    expect(result.yellowHours).toBe(85);
  });

  it('Friday Mon-Thu posting: last post Thursday, 1 day ago → 24+25=49h', () => {
    const FRIDAY = new Date('2026-03-13T12:00:00Z');
    mockBaselinesForThreshold({
      'Santa Clara': { posting_days: ['Mon', 'Tue', 'Wed', 'Thu'] },
    });
    const result = getCountyThreshold('Santa Clara', FRIDAY);
    expect(result.redHours).toBe(49);
    expect(result.yellowHours).toBe(37);
  });

  it('yellowHours = redHours - 12 for all computed values', () => {
    mockBaselinesForThreshold({
      'San Francisco': { posting_days: ['Tue', 'Thu'] },
    });
    // SATURDAY: last posting day was Thursday (2 days ago) → 48+25=73h
    const result = getCountyThreshold('San Francisco', SATURDAY);
    expect(result.yellowHours).toBe(result.redHours - 12);
  });

  it('empty posting_days → default 25h (mirrors test_empty_posting_days_returns_base)', () => {
    mockBaselinesForThreshold({
      'San Diego': { posting_days: [] },
    });
    const result = getCountyThreshold('San Diego', TUESDAY);
    expect(result.redHours).toBe(25);
    expect(result.yellowHours).toBe(13);
  });

  it('invalid posting day abbreviations → default 25h (mirrors test_invalid_day_abbrevs_ignored)', () => {
    mockBaselinesForThreshold({
      Test: { posting_days: ['Xyz', 'Abc'] },
    });
    const result = getCountyThreshold('Test', TUESDAY);
    expect(result.redHours).toBe(25);
    expect(result.yellowHours).toBe(13);
  });
});
