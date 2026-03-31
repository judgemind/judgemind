/**
 * Unit tests for courthouse reference data — department-to-courthouse mapping.
 */

import { describe, it, expect } from 'vitest';
import { lookupCourthouse, getCountiesWithCourthouseData } from '../src/reference-data/courthouses';

// ---------------------------------------------------------------------------
// lookupCourthouse
// ---------------------------------------------------------------------------

describe('lookupCourthouse', () => {
  describe('Los Angeles', () => {
    it('maps numeric departments 1-99 to Stanley Mosk Courthouse', () => {
      expect(lookupCourthouse('Los Angeles', '51')).toBe('Stanley Mosk Courthouse');
      expect(lookupCourthouse('Los Angeles', '1')).toBe('Stanley Mosk Courthouse');
      expect(lookupCourthouse('Los Angeles', '99')).toBe('Stanley Mosk Courthouse');
    });

    it('maps SS-prefix departments to Spring Street Courthouse', () => {
      expect(lookupCourthouse('Los Angeles', 'SS11')).toBe('Spring Street Courthouse');
    });

    it('maps F-prefix departments to Chatsworth Courthouse', () => {
      expect(lookupCourthouse('Los Angeles', 'F47')).toBe('Chatsworth Courthouse');
    });

    it('maps P-prefix departments to Pasadena Courthouse', () => {
      expect(lookupCourthouse('Los Angeles', 'P3')).toBe('Pasadena Courthouse');
    });
  });

  describe('Orange County', () => {
    it('maps C-prefix departments to Central Justice Center', () => {
      expect(lookupCourthouse('Orange', 'C24')).toBe('Central Justice Center, Santa Ana');
    });

    it('maps numeric departments to Central Justice Center', () => {
      expect(lookupCourthouse('Orange', '5')).toBe('Central Justice Center, Santa Ana');
    });

    it('maps L-prefix departments to Lamoreaux Justice Center', () => {
      expect(lookupCourthouse('Orange', 'L1')).toBe('Lamoreaux Justice Center');
    });

    it('maps N-prefix departments to North Justice Center', () => {
      expect(lookupCourthouse('Orange', 'N12')).toBe('North Justice Center, Fullerton');
    });
  });

  describe('Riverside', () => {
    it('maps numeric departments 1-99 to Historic Courthouse', () => {
      expect(lookupCourthouse('Riverside', '10')).toBe('Riverside Historic Courthouse');
    });

    it('maps S-prefix departments to Southwest Justice Center', () => {
      expect(lookupCourthouse('Riverside', 'S301')).toBe('Southwest Justice Center, Murrieta');
    });
  });

  describe('Santa Barbara', () => {
    it('maps numeric departments to Santa Barbara Courthouse', () => {
      expect(lookupCourthouse('Santa Barbara', '5')).toBe('Santa Barbara Courthouse');
    });

    it('maps SM-prefix departments to Santa Maria Courthouse', () => {
      expect(lookupCourthouse('Santa Barbara', 'SM2')).toBe('Santa Maria Courthouse');
    });
  });

  describe('San Bernardino', () => {
    it('maps R-prefix departments to Rancho Cucamonga Courthouse', () => {
      expect(lookupCourthouse('San Bernardino', 'R5')).toBe('Rancho Cucamonga Courthouse');
    });

    it('maps S-prefix departments to San Bernardino Justice Center', () => {
      expect(lookupCourthouse('San Bernardino', 'S24')).toBe('San Bernardino Justice Center');
    });

    it('maps V-prefix departments to Victorville Courthouse', () => {
      expect(lookupCourthouse('San Bernardino', 'V1')).toBe('Victorville Courthouse');
    });
  });

  describe('San Diego', () => {
    it('maps C-prefix departments to Central Courthouse', () => {
      expect(lookupCourthouse('San Diego', 'C-67')).toBe('Central Courthouse, San Diego');
    });

    it('maps N-prefix departments to North County', () => {
      expect(lookupCourthouse('San Diego', 'N-24')).toBe('North County Regional Center, Vista');
    });
  });

  describe('edge cases', () => {
    it('returns null for unknown county', () => {
      expect(lookupCourthouse('Fresno', '5')).toBeNull();
    });

    it('returns null for empty department', () => {
      expect(lookupCourthouse('Los Angeles', '')).toBeNull();
    });

    it('trims whitespace from department', () => {
      expect(lookupCourthouse('Los Angeles', '  51  ')).toBe('Stanley Mosk Courthouse');
    });
  });
});

// ---------------------------------------------------------------------------
// getCountiesWithCourthouseData
// ---------------------------------------------------------------------------

describe('getCountiesWithCourthouseData', () => {
  it('includes LA, OC, Riverside, SB, San Bernardino, San Diego', () => {
    const counties = getCountiesWithCourthouseData();
    expect(counties).toContain('Los Angeles');
    expect(counties).toContain('Orange');
    expect(counties).toContain('Riverside');
    expect(counties).toContain('Santa Barbara');
    expect(counties).toContain('San Bernardino');
    expect(counties).toContain('San Diego');
  });

  it('returns at least 4 counties (acceptance criterion)', () => {
    const counties = getCountiesWithCourthouseData();
    expect(counties.length).toBeGreaterThanOrEqual(4);
  });
});
