import { describe, it, expect } from 'vitest';
import { Linter } from 'eslint';

// Load the rule
// eslint-disable-next-line @typescript-eslint/no-var-requires
const rule = require('../../eslint-rules/prefer-path-alias');

// ---------------------------------------------------------------------------
// Helper: lint code with the rule using ESLint's Linter API
// ---------------------------------------------------------------------------

/**
 * The rule uses context.getCwd() to anchor path resolution. In tests we
 * set cwd to '/project' to match our fake filenames like
 * '/project/src/app/(main)/page.tsx'.
 *
 * Directory structure reference for test paths:
 *
 *   /project/
 *   └── src/
 *       ├── app/
 *       │   ├── (main)/
 *       │   │   ├── cases/
 *       │   │   │   ├── [id]/
 *       │   │   │   │   └── page.tsx
 *       │   │   │   └── page.tsx
 *       │   │   ├── rulings/
 *       │   │   └── page.tsx
 *       │   └── (auth)/
 *       ├── lib/
 *       ├── components/
 *       └── providers/
 *
 * From src/app/(main)/page.tsx:
 *   ../../lib/x     =>  (main) -> app -> src/lib/x           (2 levels = @/lib/x)
 *
 * From src/app/(main)/cases/page.tsx:
 *   ../../../lib/x  =>  cases -> (main) -> app -> src/lib/x  (3 levels = @/lib/x)
 */

const FAKE_CWD = '/project';

function lintCode(
  code: string,
  filename = '/project/src/app/(main)/page.tsx',
): Linter.LintMessage[] {
  const linter = new Linter({ cwd: FAKE_CWD });
  linter.defineRule('prefer-path-alias', rule);
  return linter.verify(
    code,
    {
      parserOptions: { ecmaVersion: 2020, sourceType: 'module' },
      rules: { 'prefer-path-alias': 'error' },
    },
    { filename },
  );
}

function lintAndFix(
  code: string,
  filename = '/project/src/app/(main)/page.tsx',
): Linter.FixReport {
  const linter = new Linter({ cwd: FAKE_CWD });
  linter.defineRule('prefer-path-alias', rule);
  return linter.verifyAndFix(
    code,
    {
      parserOptions: { ecmaVersion: 2020, sourceType: 'module' },
      rules: { 'prefer-path-alias': 'error' },
    },
    { filename },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('prefer-path-alias', () => {
  // --- Valid cases (no errors) ---

  it('allows @/ alias imports', () => {
    const messages = lintCode(
      `import { formatDate } from '@/lib/display-helpers';`,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows sibling imports (./)', () => {
    const messages = lintCode(
      `import { CaseDetail } from './CaseDetail';`,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows single parent traversal (../)', () => {
    const messages = lintCode(
      `import HomePage from '../page';`,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows package imports', () => {
    const messages = lintCode(`import { useState } from 'react';`);
    expect(messages).toHaveLength(0);
  });

  it('allows next package imports', () => {
    const messages = lintCode(`import Link from 'next/link';`);
    expect(messages).toHaveLength(0);
  });

  it('does not apply to files outside src/app/', () => {
    const messages = lintCode(
      `import { helper } from '../../lib/helpers';`,
      '/project/src/components/MyComponent.tsx',
    );
    expect(messages).toHaveLength(0);
  });

  it('ignores imports that resolve outside the project src/', () => {
    // Deep import that goes above the project's src/ directory
    const messages = lintCode(
      `import { util } from '../../../../../outside/util';`,
      '/project/src/app/(main)/page.tsx',
    );
    expect(messages).toHaveLength(0);
  });

  // --- Invalid cases (should produce errors) ---

  // From src/app/(main)/page.tsx: ../../lib/display-helpers
  //   => (main) -> app -> src/lib/display-helpers = @/lib/display-helpers
  it('reports error for ../../lib/ import in app directory', () => {
    const messages = lintCode(
      `import { formatDate } from '../../lib/display-helpers';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/lib/display-helpers');
    expect(messages[0].message).toContain('../../lib/display-helpers');
    expect(messages[0].severity).toBe(2); // error
  });

  // From src/app/(main)/cases/[id]/page.tsx: ../../../../components/ui/button
  //   => [id] -> cases -> (main) -> app -> src/components/ui/button = @/components/ui/button
  it('reports error for ../../../../components/ import in app directory', () => {
    const messages = lintCode(
      `import { Button } from '../../../../components/ui/button';`,
      '/project/src/app/(main)/cases/[id]/page.tsx',
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/components/ui/button');
  });

  // From src/app/(main)/page.tsx: ../../providers/AuthProvider
  //   => (main) -> app -> src/providers/AuthProvider = @/providers/AuthProvider
  it('reports error for ../../providers/ import in app directory', () => {
    const messages = lintCode(
      `import { AuthProvider } from '../../providers/AuthProvider';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/providers/AuthProvider');
  });

  // From src/app/(main)/cases/[id]/components/Detail.tsx: ../../../../../lib/utils
  //   => components -> [id] -> cases -> (main) -> app -> src/lib/utils = @/lib/utils
  it('reports error for deeply nested relative imports', () => {
    const messages = lintCode(
      `import { helper } from '../../../../../lib/utils';`,
      '/project/src/app/(main)/cases/[id]/components/Detail.tsx',
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/lib/utils');
  });

  it('includes the issue link in the error message', () => {
    const messages = lintCode(
      `import { x } from '../../lib/foo';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('1528');
  });

  // --- Auto-fix ---

  it('auto-fixes deep relative import to @/ alias (single quotes)', () => {
    const result = lintAndFix(
      `import { formatDate } from '../../lib/display-helpers';`,
    );
    expect(result.output).toBe(
      `import { formatDate } from '@/lib/display-helpers';`,
    );
    expect(result.fixed).toBe(true);
  });

  it('auto-fixes deep relative import to @/ alias (double quotes)', () => {
    const result = lintAndFix(
      `import { formatDate } from "../../lib/display-helpers";`,
    );
    expect(result.output).toBe(
      `import { formatDate } from "@/lib/display-helpers";`,
    );
    expect(result.fixed).toBe(true);
  });

  // From src/app/(main)/cases/[id]/page.tsx: ../../../../components/ui/button
  it('auto-fixes components path correctly', () => {
    const result = lintAndFix(
      `import { Button } from '../../../../components/ui/button';`,
      '/project/src/app/(main)/cases/[id]/page.tsx',
    );
    expect(result.output).toBe(
      `import { Button } from '@/components/ui/button';`,
    );
    expect(result.fixed).toBe(true);
  });

  it('auto-fixes providers path correctly', () => {
    const result = lintAndFix(
      `import { AuthProvider } from '../../providers/AuthProvider';`,
    );
    expect(result.output).toBe(
      `import { AuthProvider } from '@/providers/AuthProvider';`,
    );
    expect(result.fixed).toBe(true);
  });

  // --- Edge cases ---

  it('reports multiple deep relative imports in the same file', () => {
    const code = [
      `import { formatDate } from '../../lib/display-helpers';`,
      `import { Button } from '../../components/ui/button';`,
    ].join('\n');
    const messages = lintCode(code);
    expect(messages).toHaveLength(2);
  });

  it('reports error for deep relative import to a file in src root', () => {
    const messages = lintCode(
      `import { config } from '../../config';`,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('@/config');
  });

  it('auto-fixes import to src root file correctly', () => {
    const result = lintAndFix(
      `import { config } from '../../config';`,
    );
    expect(result.output).toBe(
      `import { config } from '@/config';`,
    );
    expect(result.fixed).toBe(true);
  });

  it('flags deep relative imports within app directory itself', () => {
    // ../../rulings/RulingsFeed stays within src/app/ — the rule still flags it
    // because the @/ alias is always preferred for deep relative imports
    const messages = lintCode(
      `import { RulingsFeed } from '../../rulings/RulingsFeed';`,
      '/project/src/app/(main)/cases/page.tsx',
    );
    // This resolves to @/app/rulings/RulingsFeed — still flagged
    expect(messages).toHaveLength(1);
  });
});
