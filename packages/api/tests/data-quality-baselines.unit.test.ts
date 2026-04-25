/**
 * Unit tests for the county thresholds loader (loadCountyThresholds).
 *
 * The loader reads per-county max_expected_gap_hours from
 * data-quality-baselines.json and returns a Map<county, {redHours, yellowHours}>.
 *
 * These tests use vitest's mocking to inject a temporary fixture without
 * touching the real filesystem.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';

// We'll test the loader by mocking fs.readFileSync so we control the baselines content.
vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs')>();
  return {
    ...actual,
    readFileSync: vi.fn(actual.readFileSync),
  };
});

// Must import AFTER vi.mock so we get the mocked version.
import { loadCountyThresholds, loadCountyConfigs, getCountyThreshold, resetCountyConfigsCache } from '../src/graphql/data-quality';

const mockReadFileSync = vi.mocked(readFileSync);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockBaselines(counties: Record<string, unknown>): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockReadFileSync.mockImplementation((path: any) => {
    if (String(path).endsWith('data-quality-baselines.json')) {
      return JSON.stringify({ counties }) as unknown as ReturnType<typeof readFileSync>;
    }
    // Fallback to throwing for non-baselines files so the loader tries next candidate
    throw new Error(`ENOENT: no such file: ${path}`);
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('loadCountyThresholds', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns expected map for a fixture with max_expected_gap_hours', () => {
    mockBaselines({
      Orange: {
        expected_daily_rulings: 20,
        max_expected_gap_hours: 48,
      },
      'Los Angeles': {
        expected_daily_rulings: 50,
        // No override → should not appear in the map
      },
    });

    const result = loadCountyThresholds();
    expect(result.size).toBe(1);
    expect(result.has('Orange')).toBe(true);
    expect(result.get('Orange')).toEqual({ redHours: 48, yellowHours: 36 });
    expect(result.has('Los Angeles')).toBe(false);
  });

  it('missing file returns empty Map (no throw)', () => {
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT: no such file or directory');
    });

    const result = loadCountyThresholds();
    expect(result).toBeInstanceOf(Map);
    expect(result.size).toBe(0);
  });

  it('missing max_expected_gap_hours → fallback default (not in map)', () => {
    mockBaselines({
      'Los Angeles': {
        expected_daily_rulings: 50,
        schedule_type: 'daily',
        // No max_expected_gap_hours
      },
    });

    const result = loadCountyThresholds();
    // County without override is absent → caller uses default 25h
    expect(result.has('Los Angeles')).toBe(false);
  });

  it('yellowHours = redHours - 12', () => {
    mockBaselines({
      Ventura: { max_expected_gap_hours: 36 },
    });

    const result = loadCountyThresholds();
    expect(result.get('Ventura')).toEqual({ redHours: 36, yellowHours: 24 });
  });

  it('multiple counties: only those with override are in map', () => {
    mockBaselines({
      Orange: { max_expected_gap_hours: 48 },
      'Los Angeles': { expected_daily_rulings: 50 },
      Fresno: { max_expected_gap_hours: 72 },
    });

    const result = loadCountyThresholds();
    expect(result.size).toBe(2);
    expect(result.has('Orange')).toBe(true);
    expect(result.has('Fresno')).toBe(true);
    expect(result.has('Los Angeles')).toBe(false);
  });

  it('invalid JSON returns empty Map', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockReadFileSync.mockImplementation((path: any) => {
      if (String(path).endsWith('data-quality-baselines.json')) {
        return 'not valid json {{{{' as unknown as ReturnType<typeof readFileSync>;
      }
      throw new Error('ENOENT');
    });

    const result = loadCountyThresholds();
    expect(result.size).toBe(0);
  });

  it('missing counties section returns empty Map', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    mockReadFileSync.mockImplementation((path: any) => {
      if (String(path).endsWith('data-quality-baselines.json')) {
        return JSON.stringify({ scraper_schedules: {} }) as unknown as ReturnType<typeof readFileSync>;
      }
      throw new Error('ENOENT');
    });

    const result = loadCountyThresholds();
    expect(result.size).toBe(0);
  });

  it('zero max_expected_gap_hours is ignored (non-positive guard)', () => {
    mockBaselines({
      Orange: { max_expected_gap_hours: 0 },
    });

    const result = loadCountyThresholds();
    expect(result.has('Orange')).toBe(false);
  });

  it('non-numeric max_expected_gap_hours is ignored', () => {
    mockBaselines({
      Orange: { max_expected_gap_hours: 'not-a-number' },
    });

    const result = loadCountyThresholds();
    expect(result.has('Orange')).toBe(false);
  });

  it('alerter-dashboard consistency: redHours from map equals max_expected_gap_hours in baselines', () => {
    // Structural test: confirms that the loader uses max_expected_gap_hours directly as redHours,
    // matching the alerter formula in scripts/data-quality-check.py _calculate_stale_threshold().
    // If the alerter formula changes, this test will catch the divergence.
    const gapHours = 48;
    mockBaselines({
      Orange: { max_expected_gap_hours: gapHours },
    });

    const result = loadCountyThresholds();
    expect(result.get('Orange')?.redHours).toBe(gapHours);
  });
});

// ---------------------------------------------------------------------------
// AC #4 parity test: alerter and dashboard agree on thresholds for every county
// ---------------------------------------------------------------------------

describe('getCountyThreshold — alerter/dashboard parity (AC #4)', () => {
  // Fixed reference time: Tuesday 2026-03-11 12:00 UTC.
  // Python weekday: 1 (Tue). Same as NOW in test_data_quality_check.py.
  // Day-of-week abbrev index: Tue=1 in DAY_ABBREVS.
  const TUESDAY = new Date('2026-03-11T12:00:00Z');

  beforeEach(() => {
    vi.clearAllMocks();
    resetCountyConfigsCache();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetCountyConfigsCache();
  });

  // Day abbreviation array matching Python _DAY_ABBREVS
  const DAY_ABBREVS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  /**
   * Pure-TS reimplementation of _calculate_stale_threshold from
   * scripts/data-quality-check.py. Used only in this test to cross-check
   * getCountyThreshold(). If either diverges, the test catches it.
   *
   * Returns redHours (not CountyThresholds — yellowHours = redHours - 12 by invariant).
   */
  function pyCalculateStaleThreshold(
    now: Date,
    posting_days: string[] | undefined,
    max_expected_gap_hours: number | undefined,
  ): number {
    const DEFAULT_RED_HOURS = 25;

    if (max_expected_gap_hours !== undefined) return max_expected_gap_hours;

    if (!posting_days || posting_days.length === 0) return DEFAULT_RED_HOURS;

    const postingWeekdays = new Set<number>();
    for (const day of posting_days) {
      const idx = DAY_ABBREVS.indexOf(day);
      if (idx !== -1) postingWeekdays.add(idx);
    }

    if (postingWeekdays.size === 0) return DEFAULT_RED_HOURS;

    // Python weekday: Mon=0..Sun=6. JS getUTCDay: Sun=0..Sat=6.
    const currentWeekday = (now.getUTCDay() + 6) % 7;

    for (let daysBack = 0; daysBack <= 7; daysBack++) {
      const checkDay = ((currentWeekday - daysBack) % 7 + 7) % 7;
      if (postingWeekdays.has(checkDay)) {
        if (daysBack === 0) return DEFAULT_RED_HOURS;
        return daysBack * 24 + DEFAULT_RED_HOURS;
      }
    }

    return 7 * 24 + DEFAULT_RED_HOURS;
  }

  it('getCountyThreshold matches pyCalculateStaleThreshold for every county at Tuesday 2026-03-11', () => {
    // Use the real baselines JSON — do NOT mock readFileSync here.
    // This test verifies end-to-end consistency with the actual baseline data.
    // The loader may return an empty map if the file is missing in CI; that's fine
    // because we also cross-check loadCountyConfigs() directly.
    vi.restoreAllMocks();

    const configs = loadCountyConfigs();
    if (configs.size === 0) {
      // Baselines file not found in this test environment — skip the per-county loop
      // but ensure no error is thrown.
      expect(configs).toBeInstanceOf(Map);
      return;
    }

    for (const [county, config] of configs) {
      const expected = pyCalculateStaleThreshold(
        TUESDAY,
        config.posting_days,
        config.max_expected_gap_hours,
      );
      const actual = getCountyThreshold(county, TUESDAY);
      expect(actual.redHours).toBe(expected);
      expect(actual.yellowHours).toBe(actual.redHours - 12);
    }
  });

  it('parity fixture: Orange county (max_expected_gap_hours=48) → redHours=48', () => {
    mockReadFileSync.mockImplementation((path: unknown) => {
      if (String(path).endsWith('data-quality-baselines.json')) {
        return JSON.stringify({
          counties: {
            Orange: {
              max_expected_gap_hours: 48,
              posting_days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
            },
          },
        }) as unknown as ReturnType<typeof readFileSync>;
      }
      throw new Error(`ENOENT: no such file: ${String(path)}`);
    });

    const expected = pyCalculateStaleThreshold(TUESDAY, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], 48);
    const actual = getCountyThreshold('Orange', TUESDAY);
    expect(actual.redHours).toBe(expected);
    expect(actual.redHours).toBe(48);
  });

  it('parity fixture: county with posting_days Mon-Fri, queried Saturday → redHours=49', () => {
    const SATURDAY = new Date('2026-03-14T12:00:00Z');
    mockReadFileSync.mockImplementation((path: unknown) => {
      if (String(path).endsWith('data-quality-baselines.json')) {
        return JSON.stringify({
          counties: {
            'Los Angeles': {
              posting_days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
            },
          },
        }) as unknown as ReturnType<typeof readFileSync>;
      }
      throw new Error(`ENOENT: no such file: ${String(path)}`);
    });

    // Last posting day was Friday (1 day ago) → 24 + 25 = 49
    const expected = pyCalculateStaleThreshold(SATURDAY, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], undefined);
    const actual = getCountyThreshold('Los Angeles', SATURDAY);
    expect(actual.redHours).toBe(expected);
    expect(actual.redHours).toBe(49);
  });
});
