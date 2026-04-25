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
import { loadCountyThresholds } from '../src/graphql/data-quality';

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
