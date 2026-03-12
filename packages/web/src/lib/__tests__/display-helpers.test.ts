import { describe, it, expect } from 'vitest';
import {
  buildDownloadUrl,
  detectParagraphs,
  FORMAT_LABELS,
  formatJudgeName,
  formatLabel,
  formatMotionType,
  groupParties,
  RULING_TEXT_TRUNCATE_LENGTH,
  stripBoilerplate,
  truncateText,
  cleanRulingText,
} from '../display-helpers';

// ---------------------------------------------------------------------------
// formatLabel (shared between server & client — regression test for #643)
// ---------------------------------------------------------------------------

describe('formatLabel', () => {
  it('returns em-dash for null', () => {
    expect(formatLabel(null)).toBe('\u2014');
  });

  it('converts snake_case to Title Case', () => {
    expect(formatLabel('granted_in_part')).toBe('Granted In Part');
  });

  it('uppercases known abbreviations', () => {
    expect(formatLabel('msj')).toBe('MSJ');
  });

  it('handles anti_slapp as a known compound label', () => {
    expect(formatLabel('anti_slapp')).toBe('Anti-SLAPP');
  });
});

// ---------------------------------------------------------------------------
// formatMotionType
// ---------------------------------------------------------------------------

describe('formatMotionType', () => {
  it('returns "Not classified" for null', () => {
    expect(formatMotionType(null)).toBe('Not classified');
  });

  it('handles anti_slapp as a known compound label', () => {
    expect(formatMotionType('anti_slapp')).toBe('Anti-SLAPP');
  });

  it('uppercases known abbreviations', () => {
    expect(formatMotionType('msj')).toBe('MSJ');
    expect(formatMotionType('mtd')).toBe('MTD');
    expect(formatMotionType('mil')).toBe('MIL');
  });

  it('lowercases small words except at start', () => {
    expect(formatMotionType('motion_to_dismiss')).toBe('Motion to Dismiss');
  });

  it('title-cases generic motion types', () => {
    expect(formatMotionType('summary_judgment')).toBe('Summary Judgment');
  });
});

// ---------------------------------------------------------------------------
// formatJudgeName
// ---------------------------------------------------------------------------

describe('formatJudgeName', () => {
  it('returns "Unknown judge" for null', () => {
    expect(formatJudgeName(null)).toBe('Unknown judge');
  });

  it('returns the canonical name', () => {
    expect(formatJudgeName({ canonicalName: 'Smith, John' })).toBe('Smith, John');
  });
});

// ---------------------------------------------------------------------------
// FORMAT_LABELS & buildDownloadUrl
// ---------------------------------------------------------------------------

describe('FORMAT_LABELS', () => {
  it('maps common document formats', () => {
    expect(FORMAT_LABELS['pdf']).toBe('PDF');
    expect(FORMAT_LABELS['html']).toBe('HTML');
  });
});

describe('buildDownloadUrl', () => {
  it('builds URL from NEXT_PUBLIC_GRAPHQL_URL env var', () => {
    const original = process.env.NEXT_PUBLIC_GRAPHQL_URL;
    process.env.NEXT_PUBLIC_GRAPHQL_URL = 'https://api.example.com/graphql';
    expect(buildDownloadUrl('doc-123')).toBe('https://api.example.com/api/documents/doc-123/download');
    process.env.NEXT_PUBLIC_GRAPHQL_URL = original;
  });

  it('falls back to localhost when env var is unset', () => {
    const original = process.env.NEXT_PUBLIC_GRAPHQL_URL;
    delete process.env.NEXT_PUBLIC_GRAPHQL_URL;
    expect(buildDownloadUrl('doc-456')).toBe('http://localhost:3001/api/documents/doc-456/download');
    process.env.NEXT_PUBLIC_GRAPHQL_URL = original;
  });
});

// ---------------------------------------------------------------------------
// truncateText & RULING_TEXT_TRUNCATE_LENGTH
// ---------------------------------------------------------------------------

describe('RULING_TEXT_TRUNCATE_LENGTH', () => {
  it('is 500', () => {
    expect(RULING_TEXT_TRUNCATE_LENGTH).toBe(500);
  });
});

describe('truncateText', () => {
  it('returns short text unchanged', () => {
    expect(truncateText('Hello world', 100)).toBe('Hello world');
  });

  it('truncates at word boundary', () => {
    const text = 'The quick brown fox jumps over the lazy dog';
    const result = truncateText(text, 25);
    expect(result).toContain('\u2026');
    expect(result.length).toBeLessThanOrEqual(30);
  });

  it('returns original when length equals maxLen', () => {
    expect(truncateText('exact', 5)).toBe('exact');
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
    ];
    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice', 'Carol']);
    expect(result.defendants.map((p) => p.canonicalName)).toEqual(['Bob']);
  });

  it('falls back to partyType when role is null', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: 'plaintiff', role: null },
    ];
    const result = groupParties(parties);
    expect(result.plaintiffs.map((p) => p.canonicalName)).toEqual(['Alice']);
  });

  it('handles empty list', () => {
    const result = groupParties([]);
    expect(result.plaintiffs).toEqual([]);
    expect(result.defendants).toEqual([]);
    expect(result.others).toEqual([]);
  });

  it('deduplicates parties with same id and role (#873)', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'defendant' },
      { id: '2', canonicalName: 'Bob', partyType: null, role: 'defendant' },
    ];
    const result = groupParties(parties);
    expect(result.plaintiffs).toHaveLength(1);
    expect(result.plaintiffs[0].canonicalName).toBe('Alice');
    expect(result.defendants).toHaveLength(1);
    expect(result.defendants[0].canonicalName).toBe('Bob');
  });

  it('preserves parties with same id but different roles', () => {
    const parties = [
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'plaintiff' },
      { id: '1', canonicalName: 'Alice', partyType: null, role: 'cross_defendant' },
    ];
    const result = groupParties(parties);
    expect(result.plaintiffs).toHaveLength(1);
    expect(result.defendants).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// stripBoilerplate
// ---------------------------------------------------------------------------

describe('stripBoilerplate', () => {
  it('removes single-line boilerplate headers', () => {
    const text = 'SUPERIOR COURT OF CALIFORNIA\nThe motion is granted.';
    expect(stripBoilerplate(text)).toBe('The motion is granted.');
  });

  it('removes multi-line DEPARTMENT LAW AND MOTION block', () => {
    const text =
      'DEPARTMENT 51 LAW AND MOTION RULINGS\n' +
      '1. If you wish to submit on the tentative ruling, email the clerk.\n' +
      '2. If you intend to appear, notify the court.\n' +
      '\n' +
      'The motion is granted.';
    const result = stripBoilerplate(text);
    expect(result).not.toContain('LAW AND MOTION');
    expect(result).not.toContain('wish to submit');
    expect(result).toContain('The motion is granted.');
  });

  it('removes block starting with "If you wish to submit"', () => {
    const text =
      'If you wish to submit on the tentative ruling, email the clerk.\n' +
      'Failure to do so will result in the matter being taken off calendar.\n' +
      '\n' +
      'The motion is denied.';
    const result = stripBoilerplate(text);
    expect(result).not.toContain('wish to submit');
    expect(result).toContain('The motion is denied.');
  });

  it('preserves substantive content', () => {
    const text = 'The court grants the motion for summary judgment.';
    expect(stripBoilerplate(text)).toBe(text);
  });

  it('does not delete entire text when no blank lines exist (LA ruling regression)', () => {
    // Regression test for #336: LA ruling PDFs often have no blank lines.
    // The block removal loop would consume the entire document because it
    // never found a blank line to stop at. With MAX_BLOCK_LINES=5, only
    // the header plus up to 4 following lines are removed.
    const lines = [
      'DEPARTMENT 37 LAW AND MOTION RULINGS',
      'Case Number: 25STCV34748',
      'TENTATIVE RULING',
      'Hearing Date: January 15, 2025',
      "Defendant's Motion to Quash Service of Summons",
      'The court has reviewed the moving papers filed by defendant.',
      'The motion to quash is DENIED.',
      'Defendant is ordered to file a responsive pleading within 30 days.',
      'The court finds that service was proper under CCP 415.10.',
      'Plaintiff served the summons and complaint by personal delivery.',
      'The proof of service was filed on December 1, 2024.',
      'The court rejects the argument that service was defective.',
    ];
    const text = lines.join('\n');
    const result = stripBoilerplate(text);
    // The header block should be removed but the bulk of content must survive
    expect(result).not.toContain('LAW AND MOTION');
    // Lines after the cap should be preserved
    expect(result).toContain('The court has reviewed the moving papers');
    expect(result).toContain('The motion to quash is DENIED.');
    expect(result).toContain('The court finds that service was proper');
    expect(result).toContain('The court rejects the argument');
  });

  it('caps block removal at MAX_BLOCK_LINES even with no blank lines', () => {
    // Build a document with a block header followed by 20 content lines,
    // none separated by blank lines
    const header = 'DEPARTMENT 51 LAW AND MOTION RULINGS';
    const contentLines = Array.from(
      { length: 20 },
      (_, i) => `Content line ${i + 1} of the ruling.`,
    );
    const text = [header, ...contentLines].join('\n');
    const result = stripBoilerplate(text);
    // Block removal should stop after MAX_BLOCK_LINES (5), preserving later lines
    expect(result).toContain('Content line 5 of the ruling.');
    expect(result).toContain('Content line 20 of the ruling.');
    // First 4 content lines (plus header = 5 total) should be removed
    expect(result).not.toContain('Content line 1 of the ruling.');
    expect(result).not.toContain('Content line 4 of the ruling.');
  });
});

// ---------------------------------------------------------------------------
// detectParagraphs
// ---------------------------------------------------------------------------

describe('detectParagraphs', () => {
  it('preserves text that already has double newlines', () => {
    const text = 'First paragraph.\n\nSecond paragraph.';
    expect(detectParagraphs(text)).toBe(text);
  });

  it('inserts paragraph break before ALL CAPS section headers', () => {
    const text =
      'Some introductory text about the case.\n' +
      'BACKGROUND\n' +
      'The plaintiff filed a complaint on January 1.';
    const result = detectParagraphs(text);
    expect(result).toContain('\n\nBACKGROUND\n');
  });

  it('handles multiple section headers', () => {
    const text =
      'Intro text here.\n' +
      'DISCUSSION\n' +
      'The court considers the following.\n' +
      'RULING\n' +
      'The motion is granted.';
    const result = detectParagraphs(text);
    expect(result).toContain('\n\nDISCUSSION\n');
    expect(result).toContain('\n\nRULING\n');
  });

  it('detects multi-word ALL CAPS headers', () => {
    const text =
      'Some text.\n' +
      'LEGAL STANDARD\n' +
      'The standard for summary judgment is...';
    const result = detectParagraphs(text);
    expect(result).toContain('\n\nLEGAL STANDARD\n');
  });

  it('detects indentation changes as paragraph boundaries', () => {
    const text =
      'The court rules as follows.\n' +
      '    The motion for summary judgment is granted.';
    const result = detectParagraphs(text);
    expect(result).toContain('\n\n    The motion');
  });

  it('detects tab indentation as paragraph boundary', () => {
    const text =
      'The court rules as follows.\n' +
      '\tThe motion for summary judgment is granted.';
    const result = detectParagraphs(text);
    expect(result).toContain('\n\n\tThe motion');
  });

  it('does not split mid-sentence continuations', () => {
    const text =
      'The court has reviewed the plaintiff\'s motion for summary\n' +
      'judgment and finds that there are no triable issues of\n' +
      'material fact.';
    const result = detectParagraphs(text);
    expect(result).not.toContain('\n\n');
  });

  it('returns empty string unchanged', () => {
    expect(detectParagraphs('')).toBe('');
  });

  it('returns single line unchanged', () => {
    const text = 'The motion is granted.';
    expect(detectParagraphs(text)).toBe(text);
  });

  it('treats underscore separator lines as paragraph breaks', () => {
    const text =
      'First section.\n' +
      '__________________________________\n' +
      'Second section.';
    const result = detectParagraphs(text);
    expect(result).not.toContain('__');
    expect(result).toContain('\n\n');
    expect(result).toContain('First section.');
    expect(result).toContain('Second section.');
  });

  it('treats dash separator lines as paragraph breaks', () => {
    const text =
      'Section A.\n' +
      '-----------------------------------\n' +
      'Section B.';
    const result = detectParagraphs(text);
    expect(result).not.toContain('---');
    expect(result).toContain('\n\n');
  });

  it('treats equals separator lines as paragraph breaks', () => {
    const text =
      'Above.\n' +
      '===================================\n' +
      'Below.';
    const result = detectParagraphs(text);
    expect(result).not.toContain('===');
    expect(result).toContain('\n\n');
  });

  it('does not treat short dashes as separators', () => {
    const text = 'Content.\n--\nMore content.';
    const result = detectParagraphs(text);
    expect(result).toContain('--');
  });
});

// ---------------------------------------------------------------------------
// cleanRulingText
// ---------------------------------------------------------------------------

describe('cleanRulingText', () => {
  it('produces multiple paragraphs from wall-of-text input', () => {
    const text =
      'The court considers the motion.\n' +
      'BACKGROUND\n' +
      'Plaintiff filed suit in 2024.\n' +
      'RULING\n' +
      'The motion is denied.';
    const result = cleanRulingText(text);
    // Should have at least 3 paragraphs: intro, BACKGROUND+content, RULING+content
    expect(result.length).toBeGreaterThanOrEqual(3);
  });

  it('splits on section headers', () => {
    const text =
      'Intro text.\n' +
      'DISCUSSION\n' +
      'Discussion content here.\n' +
      'RULING\n' +
      'The ruling is as follows.';
    const result = cleanRulingText(text);
    // Should contain the section headers as part of paragraphs
    const joined = result.join(' ');
    expect(joined).toContain('DISCUSSION');
    expect(joined).toContain('RULING');
    expect(result.length).toBeGreaterThanOrEqual(3);
  });

  it('preserves already-paragraphed text', () => {
    const text =
      'First paragraph with some content.\n\n' +
      'Second paragraph with more content.';
    const result = cleanRulingText(text);
    expect(result).toEqual([
      'First paragraph with some content.',
      'Second paragraph with more content.',
    ]);
  });

  it('strips multi-line boilerplate blocks from output', () => {
    const text =
      'DEPARTMENT 51 LAW AND MOTION RULINGS\n' +
      '1. If you wish to submit, email the clerk.\n' +
      '2. If you intend to appear, notify.\n' +
      '\n' +
      'The motion is granted.';
    const result = cleanRulingText(text);
    const joined = result.join(' ');
    expect(joined).not.toContain('LAW AND MOTION');
    expect(joined).not.toContain('wish to submit');
    expect(joined).toContain('The motion is granted.');
  });

  it('treats separator lines as paragraph breaks', () => {
    const text =
      'Section 1.\n' +
      '___________________________________\n' +
      'Section 2.';
    const result = cleanRulingText(text);
    expect(result.length).toBeGreaterThanOrEqual(2);
    const joined = result.join(' ');
    expect(joined).not.toContain('___');
    expect(joined).toContain('Section 1.');
    expect(joined).toContain('Section 2.');
  });

  it('preserves LA ruling text with no blank lines (#336 regression)', () => {
    // Full pipeline regression: LA rulings with DEPARTMENT header and no
    // blank lines should produce non-empty output with the ruling content.
    const lines = [
      'DEPARTMENT 37 LAW AND MOTION RULINGS',
      'Case Number: 25STCV34748',
      'TENTATIVE RULING',
      'Hearing Date: January 15, 2025',
      "Defendant's Motion to Quash Service of Summons",
      'The court has reviewed the moving papers filed by defendant.',
      'The motion to quash is DENIED.',
      'Defendant is ordered to file a responsive pleading within 30 days.',
      'The court finds that service was proper under CCP 415.10.',
      'Plaintiff served the summons and complaint by personal delivery.',
      'The proof of service was filed on December 1, 2024.',
      'The court rejects the argument that service was defective.',
    ];
    const text = lines.join('\n');
    const result = cleanRulingText(text);
    expect(result.length).toBeGreaterThan(0);
    const joined = result.join(' ');
    expect(joined).toContain('DENIED');
    expect(joined).toContain('reviewed the moving papers');
    expect(joined).not.toContain('LAW AND MOTION');
  });

  it('handles text with encoding issues and missing paragraphs', () => {
    const text =
      'The plaintiff\u00bfs motion is considered.\n' +
      'RULING\n' +
      'The motion is granted.';
    const result = cleanRulingText(text);
    // Encoding should be fixed
    const joined = result.join(' ');
    expect(joined).toContain("plaintiff's");
    // Should have paragraph breaks
    expect(result.length).toBeGreaterThanOrEqual(2);
  });
});
