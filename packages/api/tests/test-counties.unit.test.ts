/**
 * Unit tests for the test county registry.
 *
 * These tests enforce the data isolation convention: no two integration
 * test files should use the same county name in tables where it would
 * cause cross-test interference.
 */

import { describe, it, expect } from 'vitest';
import {
  TEST_COUNTY_REGISTRY,
  findCountyCollisions,
  findCourtCodeCollisions,
} from './test-counties';

describe('test county registry', () => {
  it('has no county collisions between files that share tables', () => {
    const collisions = findCountyCollisions();
    if (collisions.length > 0) {
      const details = collisions
        .map((c) => `  "${c.county}" used by: ${c.files.join(', ')}`)
        .join('\n');
      throw new Error(
        `County name collision detected — these files both insert courts with the same county:\n${details}\n` +
          'Fix: use a unique county name in one of the files, or add it to test-counties.ts with a unique court_code.',
      );
    }
  });

  it('has no duplicate court codes', () => {
    const collisions = findCourtCodeCollisions();
    if (collisions.length > 0) {
      const details = collisions
        .map((c) => `  "${c.courtCode}" used by: ${c.files.join(', ')}`)
        .join('\n');
      throw new Error(
        `Court code collision detected:\n${details}\n` +
          'Fix: use a unique court_code in one of the files.',
      );
    }
  });

  it('every registry entry has at least a counties field', () => {
    for (const [file, entry] of Object.entries(TEST_COUNTY_REGISTRY)) {
      expect(entry).toHaveProperty('counties');
      expect(entry).toHaveProperty('courtCodes');
      expect(Array.isArray(entry.counties)).toBe(true);
      expect(Array.isArray(entry.courtCodes)).toBe(true);
      // Validate that file name is non-empty
      expect(file.length).toBeGreaterThan(0);
    }
  });

  it('registry covers all known integration test files', () => {
    // This is a safeguard — if someone adds a new integration test file
    // that seeds county data, they should add it to the registry.
    const expectedFiles = [
      'graphql',
      'dataQuality',
      'searchRulings',
      'judgeAnalytics',
      'alerts',
      'auth',
    ];
    const registryFiles = Object.keys(TEST_COUNTY_REGISTRY);
    for (const expected of expectedFiles) {
      expect(registryFiles).toContain(expected);
    }
  });
});
