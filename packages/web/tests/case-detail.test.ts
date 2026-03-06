import { describe, it, expect } from 'vitest';
import {
  formatLabel,
  truncateText,
  RULING_TEXT_TRUNCATE_LENGTH,
} from '../src/app/cases/[id]/CaseDetail';

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

describe('truncateText', () => {
  it('returns the original string when shorter than maxLen', () => {
    expect(truncateText('short text', 100)).toBe('short text');
  });

  it('returns the original string when exactly maxLen', () => {
    const text = 'a'.repeat(500);
    expect(truncateText(text, 500)).toBe(text);
  });

  it('truncates long text at word boundary with ellipsis', () => {
    // Build a string longer than 500 chars with spaces
    const words = 'The court grants the motion for summary judgment. ';
    const text = words.repeat(20); // ~1000 chars
    const result = truncateText(text, 500);
    expect(result.length).toBeLessThanOrEqual(501); // 500 + ellipsis char
    expect(result).toMatch(/\u2026$/);
    // Should break at a space boundary — the text before the ellipsis should
    // not contain a partially cut word (i.e. should end at a word boundary)
    const beforeEllipsis = result.slice(0, -1);
    // The cut should happen at a space in the original text
    const cutPoint = beforeEllipsis.length;
    expect(text[cutPoint]).toBe(' ');
  });

  it('hard-cuts when no suitable space boundary exists', () => {
    const text = 'a'.repeat(600); // no spaces at all
    const result = truncateText(text, 500);
    expect(result).toBe('a'.repeat(500) + '\u2026');
  });

  it('uses default RULING_TEXT_TRUNCATE_LENGTH of 500', () => {
    expect(RULING_TEXT_TRUNCATE_LENGTH).toBe(500);
  });

  it('truncates at last space when space is in the second half', () => {
    // 400 chars of 'a', then a space, then 200 chars of 'b' = 601 total
    const text = 'a'.repeat(400) + ' ' + 'b'.repeat(200);
    const result = truncateText(text, 500);
    // Should break at the space at position 400
    expect(result).toBe('a'.repeat(400) + '\u2026');
  });
});
