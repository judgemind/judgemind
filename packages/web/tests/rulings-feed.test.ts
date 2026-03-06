import { describe, it, expect } from 'vitest';
import { formatDate, formatOutcome } from '../src/app/rulings/RulingsFeed';

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
  it('returns — for null outcome', () => {
    expect(formatOutcome(null)).toBe('—');
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
