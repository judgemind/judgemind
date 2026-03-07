import { describe, it, expect } from 'vitest';
import { buildCaseHeading, buildJudgeHeading } from '@/lib/display-helpers';

describe('@/ path alias resolution', () => {
  it('resolves @/lib imports via vitest config alias', () => {
    expect(buildCaseHeading(null, 'abc')).toBe('Case abc');
  });

  it('works for buildJudgeHeading too', () => {
    expect(buildJudgeHeading({ canonicalName: 'Smith, Jane' }, 'x')).toBe(
      'Judge Smith, Jane',
    );
  });
});
