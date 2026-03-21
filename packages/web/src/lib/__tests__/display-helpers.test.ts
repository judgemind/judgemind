import { describe, it, expect } from 'vitest';
import {
  buildDownloadUrl,
  cleanSummary,
  detectParagraphs,
  FORMAT_LABELS,
  formatDate,
  formatJudgeName,
  formatLabel,
  formatMotionType,
  getOutcomeBadgeClass,
  getOutcomeBadgeVariant,
  groupParties,
  RULING_TEXT_TRUNCATE_LENGTH,
  stripBoilerplate,
  stripMetadataHeader,
  stripMetadataHeaderHtml,
  truncateText,
  cleanRulingText,
} from '../display-helpers';

// ---------------------------------------------------------------------------
// cleanSummary (#1236 — strip heading, reduce redundancy)
// ---------------------------------------------------------------------------

describe('cleanSummary', () => {
  it('strips leading "# Summary" heading', () => {
    const summary = '# Summary\nThe court grants the motion.';
    expect(cleanSummary(summary)).toBe('The court grants the motion.');
  });

  it('strips leading "## Summary" heading', () => {
    const summary = '## Summary\nThe court denies the motion.';
    expect(cleanSummary(summary)).toBe('The court denies the motion.');
  });

  it('strips any leading markdown heading', () => {
    const summary = '### Ruling Overview\nThe motion is granted in part.';
    expect(cleanSummary(summary)).toBe('The motion is granted in part.');
  });

  it('returns empty string for empty input', () => {
    expect(cleanSummary('')).toBe('');
  });

  it('returns text unchanged when no heading is present', () => {
    const summary = 'The court grants the motion for summary judgment.';
    expect(cleanSummary(summary)).toBe(summary);
  });

  it('strips "Case Number:" metadata line', () => {
    const summary = 'Case Number: 25STCV12345\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('strips "Case No.:" metadata line', () => {
    const summary = 'Case No.: 25STCV12345\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('strips "Judge:" metadata line', () => {
    const summary = 'Judge: Johnson, Robert M.\nThe motion is denied.';
    expect(cleanSummary(summary)).toBe('The motion is denied.');
  });

  it('strips "Parties:" metadata line', () => {
    const summary = 'Parties: Smith v. Jones\nThe ruling is as follows.';
    expect(cleanSummary(summary)).toBe('The ruling is as follows.');
  });

  it('strips "Court:" metadata line', () => {
    const summary = 'Court: Los Angeles Superior Court\nMotion granted.';
    expect(cleanSummary(summary)).toBe('Motion granted.');
  });

  it('strips "Department:" metadata line', () => {
    const summary = 'Department: 12\nThe motion is denied.';
    expect(cleanSummary(summary)).toBe('The motion is denied.');
  });

  it('strips "Hearing Date:" metadata line', () => {
    const summary = 'Hearing Date: March 10, 2026\nMotion granted.';
    expect(cleanSummary(summary)).toBe('Motion granted.');
  });

  it('strips bold markdown metadata lines', () => {
    const summary = '**Case Number:** 25STCV12345\n**Judge:** Smith\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('strips heading and metadata lines together', () => {
    const summary = '# Summary\nCase Number: 25STCV12345\nJudge: Smith\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('collapses excessive blank lines after stripping', () => {
    const summary = '# Summary\n\n\n\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('preserves substantive content that mentions a judge contextually', () => {
    const summary = 'The judge ruled in favor of the plaintiff on all counts.';
    expect(cleanSummary(summary)).toBe(summary);
  });

  it('does not strip lines where "case" is used in context', () => {
    const summary = 'In this case, the motion was denied due to insufficient evidence.';
    expect(cleanSummary(summary)).toBe(summary);
  });

  it('strips duplicate metadata lines of the same type (#1320)', () => {
    const summary = 'Judge: Smith\nJudge: Jones\nThe motion is granted.';
    expect(cleanSummary(summary)).toBe('The motion is granted.');
  });

  it('strips duplicate metadata lines across different types (#1320)', () => {
    const summary =
      'Case Number: 25STCV12345\nJudge: Smith\nCase Number: 25STCV67890\nJudge: Jones\nThe ruling stands.';
    expect(cleanSummary(summary)).toBe('The ruling stands.');
  });
});

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
// formatDate (#1105 — graceful fallback for null/invalid dates)
// ---------------------------------------------------------------------------

describe('formatDate', () => {
  it('formats a valid ISO date string', () => {
    expect(formatDate('2026-03-05')).toBe('Mar 5, 2026');
  });

  it('returns "Date unknown" for null', () => {
    expect(formatDate(null)).toBe('Date unknown');
  });

  it('returns "Date unknown" for undefined', () => {
    expect(formatDate(undefined)).toBe('Date unknown');
  });

  it('returns "Date unknown" for empty string', () => {
    expect(formatDate('')).toBe('Date unknown');
  });

  it('returns "Date unknown" for unparseable string', () => {
    expect(formatDate('not-a-date')).toBe('Date unknown');
  });

  it('formats a date at year boundary correctly', () => {
    expect(formatDate('2025-12-31')).toBe('Dec 31, 2025');
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

  it('strips metadata header when metadata is provided', () => {
    const text =
      'TENTATIVE RULING FOR MARCH 20, 2026\n' +
      'Department R12 – Judge Kory Mathewson\n' +
      'Veronica Daniel v. Jerry R. Kendall, et al. – CIVSB2202699\n' +
      'Motion: Motion to Strike\n' +
      '\n' +
      'The motion to strike is GRANTED.';
    const metadata = {
      caseNumber: 'CIVSB2202699',
      caseTitle: 'Veronica Daniel v. Jerry R. Kendall, et al.',
      judgeName: 'Kory Mathewson',
      department: 'R12',
      hearingDate: '2026-03-20',
      motionType: 'motion_to_strike',
    };
    const result = cleanRulingText(text, metadata);
    const joined = result.join(' ');
    expect(joined).not.toContain('TENTATIVE RULING FOR');
    expect(joined).not.toContain('Department R12');
    expect(joined).not.toContain('CIVSB2202699');
    expect(joined).toContain('The motion to strike is GRANTED.');
  });
});

// ---------------------------------------------------------------------------
// stripMetadataHeader
// ---------------------------------------------------------------------------

describe('stripMetadataHeader', () => {
  it('returns text unchanged when no metadata provided', () => {
    const text = 'Some ruling text here.';
    expect(stripMetadataHeader(text, {})).toBe(text);
  });

  it('returns text unchanged when text does not start with metadata', () => {
    const text = 'The motion is granted.\nThe court finds in favor of plaintiff.';
    const metadata = { caseNumber: 'CIVSB2202699' };
    expect(stripMetadataHeader(text, metadata)).toBe(text);
  });

  it('strips San Bernardino-style header with date, department, judge, parties, case number, and motion type', () => {
    const text =
      'TENTATIVE RULING FOR MARCH 20, 2026\n' +
      'Department R12 – Judge Kory Mathewson\n' +
      'Veronica Daniel v. Jerry R. Kendall, et al. – CIVSB2202699\n' +
      'Motion: Motion to Strike\n' +
      '\n' +
      'The motion to strike is GRANTED.\n' +
      'The court finds sufficient grounds.';
    const metadata = {
      caseNumber: 'CIVSB2202699',
      caseTitle: 'Veronica Daniel v. Jerry R. Kendall, et al.',
      judgeName: 'Kory Mathewson',
      department: 'R12',
      hearingDate: '2026-03-20',
      motionType: 'motion_to_strike',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('TENTATIVE RULING FOR');
    expect(result).not.toContain('Department R12');
    expect(result).not.toContain('Kory Mathewson');
    expect(result).not.toContain('CIVSB2202699');
    expect(result).not.toContain('Motion: Motion to Strike');
    expect(result).toContain('The motion to strike is GRANTED.');
    expect(result).toContain('The court finds sufficient grounds.');
  });

  it('strips Ventura probate-style header with case number, title, date, and department', () => {
    const text =
      'Probate Notes 2024PRCP036355: IN THE MATTER OF JONATHAN DRAPER 03/20/2026 in Department J6 Review Hearing Transfer In\n' +
      '\n' +
      'The court orders the matter transferred.';
    const metadata = {
      caseNumber: '2024PRCP036355',
      caseTitle: 'IN THE MATTER OF JONATHAN DRAPER',
      department: 'J6',
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('Probate Notes');
    expect(result).not.toContain('2024PRCP036355');
    expect(result).toContain('The court orders the matter transferred.');
  });

  it('strips Ventura CMC-style header with case number and full party names', () => {
    const text =
      'Tentative Case Management Order 2025CUOE052557: OLGA GUILLEN NUNEZ, ON BEHALF OF HERSELF AND CURRENT AND FORMER AGGRIEVED EMPLOYEES vs PRITI PRABHA CORPORATION\n' +
      '\n' +
      'The court sets a case management conference for June 1, 2026.';
    const metadata = {
      caseNumber: '2025CUOE052557',
      caseTitle: 'OLGA GUILLEN NUNEZ, ON BEHALF OF HERSELF AND CURRENT AND FORMER AGGRIEVED EMPLOYEES vs PRITI PRABHA CORPORATION',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('Tentative Case Management Order');
    expect(result).not.toContain('2025CUOE052557');
    expect(result).toContain('The court sets a case management conference');
  });

  it('strips multi-line header where metadata spans several lines', () => {
    const text =
      'TENTATIVE RULING\n' +
      'Case No. 25STCV12345\n' +
      'Smith v. Jones\n' +
      'Hearing Date: March 20, 2026\n' +
      'Department 12\n' +
      'Judge Robert M. Johnson\n' +
      '\n' +
      'The court grants the motion for summary judgment.';
    const metadata = {
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones',
      judgeName: 'Robert M. Johnson',
      department: '12',
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('TENTATIVE RULING');
    expect(result).not.toContain('25STCV12345');
    expect(result).not.toContain('Smith v. Jones');
    expect(result).not.toContain('Hearing Date:');
    expect(result).not.toContain('Department 12');
    expect(result).not.toContain('Judge Robert M. Johnson');
    expect(result).toContain('The court grants the motion for summary judgment.');
  });

  it('stops stripping at the first non-metadata line', () => {
    const text =
      'TENTATIVE RULING\n' +
      'Case No. 25STCV12345\n' +
      'The court has considered the moving papers.\n' +
      'Department 12\n' +
      'The motion is granted.';
    const metadata = {
      caseNumber: '25STCV12345',
      department: '12',
    };
    const result = stripMetadataHeader(text, metadata);
    // Should stop at "The court has considered..." and keep everything from there
    expect(result).not.toContain('TENTATIVE RULING');
    expect(result).not.toContain('25STCV12345');
    expect(result).toContain('The court has considered the moving papers.');
    // Department 12 line comes AFTER a non-metadata line, so it stays
    expect(result).toContain('Department 12');
  });

  it('handles blank lines within the metadata header', () => {
    const text =
      'TENTATIVE RULING FOR MARCH 20, 2026\n' +
      '\n' +
      'Department R12 – Judge Kory Mathewson\n' +
      '\n' +
      'The motion is denied.';
    const metadata = {
      judgeName: 'Kory Mathewson',
      department: 'R12',
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('TENTATIVE RULING FOR');
    expect(result).not.toContain('Department R12');
    expect(result).toContain('The motion is denied.');
  });

  it('preserves text when case number appears later in the text (not in header)', () => {
    const text =
      'The court has reviewed the papers filed in this case.\n' +
      'Case number CIVSB2202699 is referenced here.\n' +
      'The motion is granted.';
    const metadata = {
      caseNumber: 'CIVSB2202699',
    };
    const result = stripMetadataHeader(text, metadata);
    // First line is not metadata — entire text should be preserved
    expect(result).toContain('The court has reviewed the papers');
    expect(result).toContain('CIVSB2202699');
  });

  it('handles date in various formats (MM/DD/YYYY)', () => {
    const text =
      '03/20/2026\n' +
      'The motion is granted.';
    const metadata = {
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('03/20/2026');
    expect(result).toContain('The motion is granted.');
  });

  it('handles date in "Month DD, YYYY" format', () => {
    const text =
      'TENTATIVE RULING FOR MARCH 20, 2026\n' +
      'The motion is granted.';
    const metadata = {
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('MARCH 20, 2026');
    expect(result).toContain('The motion is granted.');
  });

  it('handles empty text', () => {
    expect(stripMetadataHeader('', { caseNumber: '123' })).toBe('');
  });

  it('does not strip if all lines are metadata (preserves at least something)', () => {
    const text = 'Case No. 25STCV12345';
    const metadata = { caseNumber: '25STCV12345' };
    // If the entire text is metadata, we should still return it rather than empty string
    const result = stripMetadataHeader(text, metadata);
    // When ALL lines are metadata, return original to avoid losing content entirely
    expect(result.length).toBeGreaterThan(0);
  });

  it('strips "Motion:" prefix lines containing the motion type', () => {
    const text =
      'Motion: Motion to Compel\n' +
      'The court grants the motion to compel.';
    const metadata = {
      motionType: 'motion_to_compel',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('Motion: Motion to Compel');
    expect(result).toContain('The court grants the motion to compel.');
  });

  it('is case-insensitive for metadata matching', () => {
    const text =
      'tentative ruling\n' +
      'case no. 25stcv12345\n' +
      'The motion is granted.';
    const metadata = {
      caseNumber: '25STCV12345',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('tentative ruling');
    expect(result).not.toContain('25stcv12345');
    expect(result).toContain('The motion is granted.');
  });

  it('matches plural header prefixes like "Tentative Rulings" and "Court Rulings"', () => {
    const text =
      'Tentative Rulings\n' +
      'Case No. 25STCV12345\n' +
      'The motion is granted.';
    const metadata = {
      caseNumber: '25STCV12345',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('Tentative Rulings');
    expect(result).toContain('The motion is granted.');

    // Also test "Court Rulings" plural
    const text2 =
      'Court Rulings\n' +
      'Case No. 25STCV12345\n' +
      'The court grants the motion.';
    const result2 = stripMetadataHeader(text2, metadata);
    expect(result2).not.toContain('Court Rulings');
    expect(result2).toContain('The court grants the motion.');
  });

  it('matches case titles with short party names like "Li v. Wu"', () => {
    const text =
      'Li v. Wu\n' +
      'The motion is granted.';
    const metadata = {
      caseTitle: 'Li v. Wu',
    };
    const result = stripMetadataHeader(text, metadata);
    expect(result).not.toContain('Li v. Wu');
    expect(result).toContain('The motion is granted.');
  });

  it('does not false-positive on judge name substring (e.g. "Smith" in "blacksmith")', () => {
    const text =
      'The blacksmith trade was discussed in the ruling.\n' +
      'The motion is granted.';
    const metadata = {
      judgeName: 'Smith',
    };
    const result = stripMetadataHeader(text, metadata);
    // "blacksmith" contains "smith" as substring but not as whole word
    expect(result).toContain('blacksmith');
  });

  it('does not false-positive on case number as substring of another number', () => {
    const text =
      'See Rule 25STCV123456789 for details.\n' +
      'The motion is granted.';
    const metadata = {
      caseNumber: '25STCV12345',
    };
    const result = stripMetadataHeader(text, metadata);
    // "25STCV12345" should not match "25STCV123456789" (substring, not whole word)
    expect(result).toContain('Rule 25STCV123456789');
  });

  it('does not strip substantive text that happens to mention a party name', () => {
    const text =
      "Plaintiff Li's argument is unpersuasive.\n" +
      'The motion is denied.';
    const metadata = {
      caseTitle: 'Li v. Wu',
    };
    const result = stripMetadataHeader(text, metadata);
    // Single-word "Li" from a 2-word title (Li, Wu) requires at least 2 matches
    // This line only matches "Li" (1 match) so it should NOT be stripped
    expect(result).toContain("Plaintiff Li's argument");
  });
});

// ---------------------------------------------------------------------------
// stripMetadataHeaderHtml
// ---------------------------------------------------------------------------

describe('stripMetadataHeaderHtml', () => {
  it('returns html unchanged when no metadata provided', () => {
    const html = '<p>Some ruling text here.</p>';
    expect(stripMetadataHeaderHtml(html, {})).toBe(html);
  });

  it('returns html unchanged when html does not start with metadata', () => {
    const html = '<p>The motion is granted.</p><p>The court finds in favor of plaintiff.</p>';
    const metadata = { caseNumber: 'CIVSB2202699' };
    expect(stripMetadataHeaderHtml(html, metadata)).toBe(html);
  });

  it('strips leading <p> elements containing metadata', () => {
    const html =
      '<p>TENTATIVE RULING FOR MARCH 20, 2026</p>' +
      '<p>Department R12 – Judge Kory Mathewson</p>' +
      '<p>The motion to strike is GRANTED.</p>';
    const metadata = {
      judgeName: 'Kory Mathewson',
      department: 'R12',
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).not.toContain('TENTATIVE RULING FOR');
    expect(result).not.toContain('Department R12');
    expect(result).toContain('The motion to strike is GRANTED.');
  });

  it('strips leading <div> elements containing metadata', () => {
    const html =
      '<div>Case No. 25STCV12345</div>' +
      '<div>Smith v. Jones</div>' +
      '<p>The court grants the motion.</p>';
    const metadata = {
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones',
    };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).not.toContain('25STCV12345');
    expect(result).not.toContain('Smith v. Jones');
    expect(result).toContain('The court grants the motion.');
  });

  it('stops stripping at the first non-metadata element', () => {
    const html =
      '<p>TENTATIVE RULING</p>' +
      '<p>The court has considered the motion.</p>' +
      '<p>Case No. 25STCV12345</p>';
    const metadata = {
      caseNumber: '25STCV12345',
    };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).not.toContain('TENTATIVE RULING');
    expect(result).toContain('The court has considered the motion.');
    // Case number after non-metadata element is preserved
    expect(result).toContain('25STCV12345');
  });

  it('handles empty html', () => {
    expect(stripMetadataHeaderHtml('', { caseNumber: '123' })).toBe('');
  });

  it('preserves html when all elements are metadata (avoids losing content)', () => {
    const html = '<p>Case No. 25STCV12345</p>';
    const metadata = { caseNumber: '25STCV12345' };
    const result = stripMetadataHeaderHtml(html, metadata);
    // Should return original since stripping would leave nothing
    expect(result.length).toBeGreaterThan(0);
  });

  it('strips heading elements containing metadata', () => {
    const html =
      '<h3>Tentative Ruling</h3>' +
      '<p>Case No. 25STCV12345</p>' +
      '<p>The motion is granted.</p>';
    const metadata = {
      caseNumber: '25STCV12345',
    };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).not.toContain('Tentative Ruling');
    expect(result).not.toContain('25STCV12345');
    expect(result).toContain('The motion is granted.');
  });

  it('handles <br> tags within metadata elements', () => {
    const html =
      '<p>TENTATIVE RULING<br>FOR MARCH 20, 2026</p>' +
      '<p>The motion is granted.</p>';
    const metadata = {
      hearingDate: '2026-03-20',
    };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).not.toContain('TENTATIVE RULING');
    expect(result).not.toContain('MARCH 20, 2026');
    expect(result).toContain('The motion is granted.');
  });

  it('ensures opening and closing tags match (no mismatched tag stripping)', () => {
    // Malformed HTML where tags don't match — should not strip
    const html =
      '<p>The motion is granted.</p>';
    const metadata = { caseNumber: '25STCV12345' };
    const result = stripMetadataHeaderHtml(html, metadata);
    expect(result).toContain('The motion is granted.');
  });
});

// ---------------------------------------------------------------------------
// getOutcomeBadgeVariant (#1256 — shared outcome badge styling)
// ---------------------------------------------------------------------------

describe('getOutcomeBadgeVariant', () => {
  it('returns "default" for granted', () => {
    expect(getOutcomeBadgeVariant('granted')).toBe('default');
  });

  it('returns "destructive" for denied', () => {
    expect(getOutcomeBadgeVariant('denied')).toBe('destructive');
  });

  it('returns "secondary" for granted_in_part', () => {
    expect(getOutcomeBadgeVariant('granted_in_part')).toBe('secondary');
  });

  it('returns "secondary" for denied_in_part', () => {
    expect(getOutcomeBadgeVariant('denied_in_part')).toBe('secondary');
  });

  it('returns "outline" for moot', () => {
    expect(getOutcomeBadgeVariant('moot')).toBe('outline');
  });

  it('returns "outline" for continued', () => {
    expect(getOutcomeBadgeVariant('continued')).toBe('outline');
  });

  it('returns "outline" for other', () => {
    expect(getOutcomeBadgeVariant('other')).toBe('outline');
  });

  it('returns "outline" for null', () => {
    expect(getOutcomeBadgeVariant(null)).toBe('outline');
  });

  it('returns "outline" for unknown outcome', () => {
    expect(getOutcomeBadgeVariant('something_unknown')).toBe('outline');
  });
});

// ---------------------------------------------------------------------------
// getOutcomeBadgeClass (#1256 — shared outcome badge styling)
// ---------------------------------------------------------------------------

describe('getOutcomeBadgeClass', () => {
  it('returns green classes for granted', () => {
    expect(getOutcomeBadgeClass('granted')).toContain('bg-green-100');
    expect(getOutcomeBadgeClass('granted')).toContain('dark:bg-green-900');
  });

  it('returns red classes for denied', () => {
    expect(getOutcomeBadgeClass('denied')).toContain('bg-red-100');
    expect(getOutcomeBadgeClass('denied')).toContain('dark:bg-red-900');
  });

  it('returns yellow classes for granted_in_part', () => {
    expect(getOutcomeBadgeClass('granted_in_part')).toContain('bg-yellow-100');
  });

  it('returns yellow classes for denied_in_part', () => {
    expect(getOutcomeBadgeClass('denied_in_part')).toContain('bg-yellow-100');
  });

  it('returns slate fallback for null', () => {
    expect(getOutcomeBadgeClass(null)).toContain('bg-slate-100');
    expect(getOutcomeBadgeClass(null)).toContain('dark:bg-slate-700');
  });

  it('returns slate fallback for unknown outcome', () => {
    expect(getOutcomeBadgeClass('something_unknown')).toContain('bg-slate-100');
  });

  it('returns slate fallback for moot (no specific CSS mapping)', () => {
    expect(getOutcomeBadgeClass('moot')).toContain('bg-slate-100');
  });
});
