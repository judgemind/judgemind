import { describe, it, expect } from 'vitest';
import { Linter } from 'eslint';

// Load the rule
// eslint-disable-next-line @typescript-eslint/no-var-requires
const rule = require('../../eslint-rules/no-ssr-incompatible-import');

// ---------------------------------------------------------------------------
// Helper: lint code with the rule using ESLint's Linter API
// ---------------------------------------------------------------------------

function lintCode(code: string): Linter.LintMessage[] {
  const linter = new Linter();
  linter.defineRule('no-ssr-incompatible-import', rule);
  return linter.verify(
    code,
    {
      parserOptions: { ecmaVersion: 2020, sourceType: 'module' },
      rules: { 'no-ssr-incompatible-import': 'error' },
    },
    { filename: '/fake/src/app/rulings/page.tsx' },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('no-ssr-incompatible-import', () => {
  // --- Valid cases (no errors) ---

  it('allows importing from @/lib/sanitize-html (the approved wrapper)', () => {
    const messages = lintCode(
      `import { sanitizeRulingHtml } from '@/lib/sanitize-html';`,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows importing regular packages like react', () => {
    const messages = lintCode(`import { useState } from 'react';`);
    expect(messages).toHaveLength(0);
  });

  it('allows importing next packages', () => {
    const messages = lintCode(`import Link from 'next/link';`);
    expect(messages).toHaveLength(0);
  });

  it('allows importing relative modules', () => {
    const messages = lintCode(`import { MyComponent } from './MyComponent';`);
    expect(messages).toHaveLength(0);
  });

  it('allows importing dompurify (client-only, not isomorphic)', () => {
    const messages = lintCode(`import DOMPurify from 'dompurify';`);
    expect(messages).toHaveLength(0);
  });

  // --- Invalid cases (should produce errors) ---

  it('reports error for direct isomorphic-dompurify default import', () => {
    const messages = lintCode(
      `import DOMPurify from 'isomorphic-dompurify';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
    expect(messages[0].message).toContain('@/lib/sanitize-html');
    expect(messages[0].severity).toBe(2); // error
  });

  it('reports error for named import from isomorphic-dompurify', () => {
    const messages = lintCode(
      `import { sanitize } from 'isomorphic-dompurify';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  it('reports error for namespace import from isomorphic-dompurify', () => {
    const messages = lintCode(
      `import * as DOMPurify from 'isomorphic-dompurify';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  it('reports error for side-effect import of isomorphic-dompurify', () => {
    const messages = lintCode(`import 'isomorphic-dompurify';`);
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  it('error message includes the suggested alternative', () => {
    const messages = lintCode(
      `import DOMPurify from 'isomorphic-dompurify';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/lib/sanitize-html');
    expect(messages[0].message).toContain('SSR');
  });

  it('only flags the banned import, not other imports in the same file', () => {
    const code = [
      `import { useState } from 'react';`,
      `import DOMPurify from 'isomorphic-dompurify';`,
      `import Link from 'next/link';`,
    ].join('\n');
    const messages = lintCode(code);
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  // --- require() detection ---

  it('reports error for require of isomorphic-dompurify', () => {
    const messages = lintCode(
      `const DOMPurify = require('isomorphic-dompurify');`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  it('reports error for bare require of isomorphic-dompurify', () => {
    const messages = lintCode(`require('isomorphic-dompurify');`);
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('isomorphic-dompurify');
  });

  it('allows require of non-banned packages', () => {
    const messages = lintCode(`const fs = require('fs');`);
    expect(messages).toHaveLength(0);
  });
});
