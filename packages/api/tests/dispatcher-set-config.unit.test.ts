/**
 * Unit tests for `validateConfigValue` — the per-key config editor
 * validator used by the `dispatcherSetConfig` mutation (#2805 §1.6).
 */

import { describe, expect, it } from 'vitest';
import { validateConfigValue } from '../src/graphql/dispatcher/resolvers';

describe('validateConfigValue — concurrency_cap', () => {
  it('accepts integers in [0, 5]', () => {
    expect(validateConfigValue('concurrency_cap', 0)).toBe(0);
    expect(validateConfigValue('concurrency_cap', 3)).toBe(3);
    expect(validateConfigValue('concurrency_cap', 5)).toBe(5);
  });

  it('rejects negatives', () => {
    expect(() => validateConfigValue('concurrency_cap', -1)).toThrow(/integer in \[0, 5\]/);
  });

  it('rejects values > 5', () => {
    expect(() => validateConfigValue('concurrency_cap', 6)).toThrow();
    expect(() => validateConfigValue('concurrency_cap', 100)).toThrow();
  });

  it('rejects non-integers', () => {
    expect(() => validateConfigValue('concurrency_cap', 2.5)).toThrow();
    expect(() => validateConfigValue('concurrency_cap', 'three')).toThrow();
    expect(() => validateConfigValue('concurrency_cap', null)).toThrow();
  });
});

describe('validateConfigValue — backoff_seconds', () => {
  it('accepts arrays of non-negative integers', () => {
    expect(validateConfigValue('backoff_seconds', [60, 300, 900])).toEqual([60, 300, 900]);
    expect(validateConfigValue('backoff_seconds', [0])).toEqual([0]);
    expect(validateConfigValue('backoff_seconds', [])).toEqual([]);
  });

  it('rejects arrays with negatives', () => {
    expect(() => validateConfigValue('backoff_seconds', [60, -1])).toThrow();
  });

  it('rejects arrays with non-integers', () => {
    expect(() => validateConfigValue('backoff_seconds', [60, 'slow'])).toThrow();
    expect(() => validateConfigValue('backoff_seconds', [60, 1.5])).toThrow();
  });

  it('rejects non-array input', () => {
    expect(() => validateConfigValue('backoff_seconds', 60)).toThrow();
    expect(() => validateConfigValue('backoff_seconds', '60,300,900')).toThrow();
  });
});

describe('validateConfigValue — idle_spotcheck_cron', () => {
  it('accepts non-empty strings', () => {
    expect(validateConfigValue('idle_spotcheck_cron', '0 14 * * *')).toBe('0 14 * * *');
  });

  it('rejects empty / non-string', () => {
    expect(() => validateConfigValue('idle_spotcheck_cron', '')).toThrow();
    expect(() => validateConfigValue('idle_spotcheck_cron', 42)).toThrow();
  });
});

describe('validateConfigValue — unknown key', () => {
  it('rejects freehand keys', () => {
    expect(() => validateConfigValue('new_key', 'anything')).toThrow(/Unknown config key/);
  });
});
