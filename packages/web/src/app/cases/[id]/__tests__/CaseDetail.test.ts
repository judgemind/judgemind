import { describe, it, expect } from 'vitest';
import { formatLabel, truncateText, groupParties } from '../CaseDetail';

// ---------------------------------------------------------------------------
// formatLabel
// ---------------------------------------------------------------------------

describe('formatLabel', () => {
  it('returns em-dash for null input', () => {
    expect(formatLabel(null)).toBe('\u2014');
  });

  it('converts snake_case to Title Case', () => {
    expect(formatLabel('summary_judgment')).toBe('Summary Judgment');
  });

  it('uppercases known abbreviations', () => {
    expect(formatLabel('msj')).toBe('MSJ');
    expect(formatLabel('mtd')).toBe('MTD');
    expect(formatLabel('mil')).toBe('MIL');
  });

  it('handles anti_slapp as a known compound label', () => {
    expect(formatLabel('anti_slapp')).toBe('Anti-SLAPP');
  });

  it('handles single word', () => {
    expect(formatLabel('demurrer')).toBe('Demurrer');
  });
});

// ---------------------------------------------------------------------------
// truncateText
// ---------------------------------------------------------------------------

describe('truncateText', () => {
  it('returns short text unchanged', () => {
    const text = 'Hello world';
    expect(truncateText(text, 100)).toBe(text);
  });

  it('truncates at word boundary', () => {
    const text = 'The quick brown fox jumps over the lazy dog';
    const result = truncateText(text, 25);
    expect(result.length).toBeLessThanOrEqual(30); // max + ellipsis char
    expect(result).toContain('\u2026');
  });

  it('returns original when length equals maxLen', () => {
    const text = 'exact';
    expect(truncateText(text, 5)).toBe(text);
  });
});

// ---------------------------------------------------------------------------
// groupParties
// ---------------------------------------------------------------------------

describe('groupParties', () => {
  it('groups by role when role is present', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'defendant' },
      { id: '3', canonicalName: 'Carol', partyType: null, role: 'petitioner' },
      { id: '4', canonicalName: 'Dave', partyType: null, role: 'respondent' },
      { id: '5', canonicalName: 'Eve', partyType: null, role: 'witness' },
      { id: '6', canonicalName: 'Frank', partyType: null, role: null },
    ];

    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual([
      'Alice',
      'Carol',
    ]);
    expect(result.defendants.map((p) => p.canonicalName)).toEqual([
      'Bob',
      'Dave',
    ]);
    expect(result.others.map((p) => p.canonicalName)).toEqual([
      'Eve',
      'Frank',
    ]);
  });

  it('falls back to partyType when role is null', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: 'plaintiff', role: null },
      { id: '2', canonicalName: 'Bob', partyType: 'defendant', role: null },
    ];

    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice']);
    expect(result.defendants.map((p) => p.canonicalName)).toEqual(['Bob']);
  });

  it('role takes precedence over partyType', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: 'defendant', role: 'plaintiff' },
    ];

    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice']);
    expect(result.defendants).toEqual([]);
  });

  it('groups moving_party as plaintiff and responding_party as defendant', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'moving_party' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'responding_party' },
    ];

    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice']);
    expect(result.defendants.map((p) => p.canonicalName)).toEqual(['Bob']);
  });

  it('groups cross_complainant and cross_defendant correctly', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'cross_complainant' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'cross_defendant' },
    ];

    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice']);
    expect(result.defendants.map((p) => p.canonicalName)).toEqual(['Bob']);
  });

  it('handles empty list', () => {
    const result = groupParties([]);
    expect(result.plaintiffs).toEqual([]);
    expect(result.defendants).toEqual([]);
    expect(result.others).toEqual([]);
  });
});
