import { describe, it, expect } from 'vitest';
import {
  formatLabel,
  truncateText,
  groupParties,
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

  it('keeps MSJ uppercase', () => {
    expect(formatLabel('msj')).toBe('MSJ');
    expect(formatLabel('MSJ')).toBe('MSJ');
  });

  it('keeps MTD uppercase', () => {
    expect(formatLabel('mtd')).toBe('MTD');
  });

  it('keeps MIL uppercase', () => {
    expect(formatLabel('mil')).toBe('MIL');
  });

  it('formats anti_slapp as Anti-SLAPP', () => {
    expect(formatLabel('anti_slapp')).toBe('Anti-SLAPP');
    expect(formatLabel('ANTI_SLAPP')).toBe('Anti-SLAPP');
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

describe('groupParties', () => {
  const mkParty = (id: string, name: string, type: string | null) => ({
    id,
    canonicalName: name,
    partyType: type,
  });

  it('groups plaintiffs and defendants correctly', () => {
    const parties = [
      mkParty('1', 'Alice Smith', 'plaintiff'),
      mkParty('2', 'Bob Jones', 'defendant'),
      mkParty('3', 'Carol White', 'plaintiff'),
    ];
    const { plaintiffs, defendants, others } = groupParties(parties);
    expect(plaintiffs).toHaveLength(2);
    expect(defendants).toHaveLength(1);
    expect(others).toHaveLength(0);
    expect(plaintiffs[0].canonicalName).toBe('Alice Smith');
    expect(defendants[0].canonicalName).toBe('Bob Jones');
  });

  it('groups petitioners as plaintiffs and respondents as defendants', () => {
    const parties = [
      mkParty('1', 'Alice', 'petitioner'),
      mkParty('2', 'Bob', 'respondent'),
    ];
    const { plaintiffs, defendants } = groupParties(parties);
    expect(plaintiffs).toHaveLength(1);
    expect(defendants).toHaveLength(1);
  });

  it('groups cross_complainant as plaintiff and cross_defendant as defendant', () => {
    const parties = [
      mkParty('1', 'Alice', 'cross_complainant'),
      mkParty('2', 'Bob', 'cross_defendant'),
    ];
    const { plaintiffs, defendants } = groupParties(parties);
    expect(plaintiffs).toHaveLength(1);
    expect(defendants).toHaveLength(1);
  });

  it('puts unrecognized party types in others', () => {
    const parties = [
      mkParty('1', 'Witness', 'witness'),
      mkParty('2', 'Intervenor', 'intervenor'),
    ];
    const { plaintiffs, defendants, others } = groupParties(parties);
    expect(plaintiffs).toHaveLength(0);
    expect(defendants).toHaveLength(0);
    expect(others).toHaveLength(2);
  });

  it('puts null party types in others', () => {
    const parties = [mkParty('1', 'Unknown', null)];
    const { others } = groupParties(parties);
    expect(others).toHaveLength(1);
  });

  it('returns empty arrays for no parties', () => {
    const { plaintiffs, defendants, others } = groupParties([]);
    expect(plaintiffs).toHaveLength(0);
    expect(defendants).toHaveLength(0);
    expect(others).toHaveLength(0);
  });
});
