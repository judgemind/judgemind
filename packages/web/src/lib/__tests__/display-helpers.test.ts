import { describe, it, expect } from 'vitest';
import {
  detectParagraphs,
  cleanRulingText,
} from '../display-helpers';

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
