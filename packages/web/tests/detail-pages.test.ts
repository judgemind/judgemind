import { describe, it, expect } from 'vitest';
import {
  buildCaseHeading,
  buildJudgeHeading,
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
