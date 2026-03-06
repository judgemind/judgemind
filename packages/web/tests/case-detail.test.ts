import { describe, it, expect } from 'vitest';
import { formatLabel } from '../src/app/cases/[id]/CaseDetail';

describe('formatLabel', () => {
  it('returns em-dash for null', () => {
    expect(formatLabel(null)).toBe('\u2014');
  });

  it('formats a single word to title case', () => {
    expect(formatLabel('civil')).toBe('Civil');
  });

  it('formats snake_case to Title Case with spaces', () => {
    expect(formatLabel('granted_in_part')).toBe('Granted In Part');
  });

  it('formats "active" correctly', () => {
    expect(formatLabel('active')).toBe('Active');
  });

  it('formats "cross_defendant" correctly', () => {
    expect(formatLabel('cross_defendant')).toBe('Cross Defendant');
  });

  it('formats an already-title-case word', () => {
    expect(formatLabel('Criminal')).toBe('Criminal');
  });

  it('returns em-dash for empty string', () => {
    // empty string is falsy in JS
    expect(formatLabel('')).toBe('\u2014');
  });
});
