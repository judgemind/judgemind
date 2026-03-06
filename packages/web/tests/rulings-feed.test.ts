import { describe, it, expect } from 'vitest';
import {
  formatDate,
  formatOutcome,
  formatMotionType,
  formatJudgeName,
} from '../src/app/rulings/RulingsFeed';

describe('formatDate', () => {
  it('formats an ISO date as a short readable date', () => {
    expect(formatDate('2026-03-05')).toBe('Mar 5, 2026');
  });

  it('formats the first day of a month correctly', () => {
    expect(formatDate('2026-01-01')).toBe('Jan 1, 2026');
  });

  it('formats end of year correctly', () => {
    expect(formatDate('2025-12-31')).toBe('Dec 31, 2025');
  });
});

describe('formatOutcome', () => {
  it('returns "Not classified" for null outcome', () => {
    expect(formatOutcome(null)).toBe('Not classified');
  });

  it('formats granted', () => {
    expect(formatOutcome('granted')).toBe('Granted');
  });

  it('formats denied', () => {
    expect(formatOutcome('denied')).toBe('Denied');
  });

  it('formats snake_case outcomes with spaces and title case', () => {
    expect(formatOutcome('granted_in_part')).toBe('Granted In Part');
  });

  it('formats denied_in_part', () => {
    expect(formatOutcome('denied_in_part')).toBe('Denied In Part');
  });

  it('formats off_calendar', () => {
    expect(formatOutcome('off_calendar')).toBe('Off Calendar');
  });
});

describe('formatMotionType', () => {
  it('returns "Not classified" for null motion type', () => {
    expect(formatMotionType(null)).toBe('Not classified');
  });

  it('formats lowercase snake_case to Title Case', () => {
    expect(formatMotionType('motion_to_compel')).toBe('Motion to Compel');
  });

  it('formats UPPER_CASE snake_case to Title Case', () => {
    expect(formatMotionType('MOTION_TO_COMPEL')).toBe('Motion to Compel');
  });

  it('formats single-word types to Title Case', () => {
    expect(formatMotionType('demurrer')).toBe('Demurrer');
    expect(formatMotionType('DEMURRER')).toBe('Demurrer');
  });

  it('keeps MSJ uppercase', () => {
    expect(formatMotionType('msj')).toBe('MSJ');
    expect(formatMotionType('MSJ')).toBe('MSJ');
  });

  it('keeps MTD uppercase', () => {
    expect(formatMotionType('mtd')).toBe('MTD');
    expect(formatMotionType('MTD')).toBe('MTD');
  });

  it('keeps MIL uppercase', () => {
    expect(formatMotionType('mil')).toBe('MIL');
    expect(formatMotionType('MIL')).toBe('MIL');
  });

  it('formats anti_slapp as Anti-SLAPP', () => {
    expect(formatMotionType('anti_slapp')).toBe('Anti-SLAPP');
    expect(formatMotionType('ANTI_SLAPP')).toBe('Anti-SLAPP');
  });

  it('formats motion_to_strike correctly', () => {
    expect(formatMotionType('motion_to_strike')).toBe('Motion to Strike');
  });
});

describe('formatJudgeName', () => {
  it('returns "Unknown judge" for null judge', () => {
    expect(formatJudgeName(null)).toBe('Unknown judge');
  });

  it('returns the canonical name when judge is present', () => {
    expect(formatJudgeName({ canonicalName: 'Smith, John' })).toBe(
      'Smith, John',
    );
  });

  it('returns the canonical name for a different judge', () => {
    expect(formatJudgeName({ canonicalName: 'Doe, Jane M.' })).toBe(
      'Doe, Jane M.',
    );
  });
});
