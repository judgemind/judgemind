import { describe, it, expect } from 'vitest';
import {
  buildCaseHeading,
  buildJudgeHeading,
  fixEncoding,
  stripPageNumbers,
  cleanRulingText,
} from '../src/lib/display-helpers';

describe('buildCaseHeading', () => {
  it('shows only the case title when both title and number are present', () => {
    const result = buildCaseHeading(
      { caseNumber: '23STCV12345', caseTitle: 'Smith v. Jones' },
      'some-uuid',
    );
    expect(result).toBe('Smith v. Jones');
  });

  it('shows only case number when title is null', () => {
    const result = buildCaseHeading(
      { caseNumber: '23STCV12345', caseTitle: null },
      'some-uuid',
    );
    expect(result).toBe('23STCV12345');
  });

  it('falls back to "Case {id}" when case data is null', () => {
    const result = buildCaseHeading(null, 'abc-123');
    expect(result).toBe('Case abc-123');
  });
});

describe('buildJudgeHeading', () => {
  it('shows "Judge {canonicalName}" when data is present', () => {
    const result = buildJudgeHeading(
      { canonicalName: 'Johnson, Robert M.' },
      'some-uuid',
    );
    expect(result).toBe('Judge Johnson, Robert M.');
  });

  it('falls back to "Judge {id}" when judge data is null', () => {
    const result = buildJudgeHeading(null, 'abc-123');
    expect(result).toBe('Judge abc-123');
  });
});

describe('fixEncoding', () => {
  it('fixes inverted question mark to apostrophe', () => {
    expect(fixEncoding('plaintiff\u00bfs')).toBe("plaintiff's");
  });

  it('fixes smart double quotes', () => {
    expect(fixEncoding('\u00e2\u0080\u009cHello\u00e2\u0080\u009d')).toBe('\u201cHello\u201d');
  });

  it('fixes en dash', () => {
    expect(fixEncoding('1\u00e2\u0080\u009310')).toBe('1\u201310');
  });

  it('normalizes non-breaking spaces', () => {
    expect(fixEncoding('hello\u00a0world')).toBe('hello world');
  });

  it('preserves clean text', () => {
    const text = 'The court grants the motion.';
    expect(fixEncoding(text)).toBe(text);
  });
});

describe('stripPageNumbers', () => {
  it('removes Page X of Y lines', () => {
    expect(stripPageNumbers('Text\nPage 2 of 5\nMore')).toBe('Text\nMore');
  });

  it('removes dash-number-dash lines', () => {
    expect(stripPageNumbers('Text\n- 3 -\nMore')).toBe('Text\nMore');
  });

  it('removes standalone small numbers', () => {
    expect(stripPageNumbers('Text\n42\nMore')).toBe('Text\nMore');
  });

  it('preserves numbers within text', () => {
    const text = 'The court awarded 42 days.';
    expect(stripPageNumbers(text)).toBe(text);
  });
});

describe('cleanRulingText', () => {
  it('splits text into paragraphs on double-newlines', () => {
    const text = 'Paragraph one.\n\nParagraph two.';
    expect(cleanRulingText(text)).toEqual(['Paragraph one.', 'Paragraph two.']);
  });

  it('joins single-newline-separated lines within a paragraph', () => {
    const text = 'Line one of a\nlong paragraph.';
    expect(cleanRulingText(text)).toEqual(['Line one of a long paragraph.']);
  });

  it('removes page number lines', () => {
    const text = 'Content.\n\nPage 1 of 3\n\nMore content.';
    const result = cleanRulingText(text);
    expect(result).toContain('Content.');
    expect(result).toContain('More content.');
    expect(result.join(' ')).not.toContain('Page 1 of 3');
  });

  it('fixes encoding errors', () => {
    const text = 'plaintiff\u00bfs motion';
    const result = cleanRulingText(text);
    expect(result[0]).toBe("plaintiff's motion");
  });

  it('filters out empty paragraphs', () => {
    const text = '\n\n\n\nContent.\n\n\n\n';
    expect(cleanRulingText(text)).toEqual(['Content.']);
  });

  it('returns empty array for whitespace-only input', () => {
    expect(cleanRulingText('   \n\n   ')).toEqual([]);
  });

  it('handles realistic ruling text with multiple issues', () => {
    const raw = [
      'The court has reviewed plaintiff\u00bfs motion.',
      '',
      'Page 1 of 2',
      '',
      'The motion is GRANTED.',
      '- 2 -',
    ].join('\n');
    const result = cleanRulingText(raw);
    expect(result.join(' ')).toContain("plaintiff's motion");
    expect(result.join(' ')).toContain('GRANTED');
    expect(result.join(' ')).not.toContain('Page 1 of 2');
    expect(result.join(' ')).not.toContain('- 2 -');
  });
});
