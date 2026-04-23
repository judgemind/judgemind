import { describe, it, expect } from 'vitest';
import { PAGE_TITLE, SECTION_HEADING, SECTION_LABEL } from '../typography';

describe('typography constants', () => {
  it('exports PAGE_TITLE with the correct classes', () => {
    expect(PAGE_TITLE).toBe(
      'text-2xl font-bold tracking-tight text-foreground',
    );
  });

  it('exports SECTION_HEADING with the correct classes', () => {
    expect(SECTION_HEADING).toBe('text-lg font-semibold text-foreground');
  });

  it('exports SECTION_LABEL with the correct classes', () => {
    expect(SECTION_LABEL).toBe(
      'text-sm font-semibold uppercase tracking-wide text-muted-foreground',
    );
  });

  it('PAGE_TITLE includes text-2xl for large size', () => {
    expect(PAGE_TITLE).toContain('text-2xl');
  });

  it('PAGE_TITLE includes tracking-tight', () => {
    expect(PAGE_TITLE).toContain('tracking-tight');
  });

  it('SECTION_LABEL includes uppercase and tracking-wide', () => {
    expect(SECTION_LABEL).toContain('uppercase');
    expect(SECTION_LABEL).toContain('tracking-wide');
  });

  it('SECTION_LABEL uses muted-foreground color', () => {
    expect(SECTION_LABEL).toContain('text-muted-foreground');
  });

  it('PAGE_TITLE and SECTION_HEADING use foreground color', () => {
    expect(PAGE_TITLE).toContain('text-foreground');
    expect(SECTION_HEADING).toContain('text-foreground');
  });
});
