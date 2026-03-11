import { describe, it, expect, afterAll } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { Linter } from 'eslint';

// Load the rule
// eslint-disable-next-line @typescript-eslint/no-var-requires
const rule = require('../../eslint-rules/no-client-utility-import');

// ---------------------------------------------------------------------------
// Test fixture setup
// ---------------------------------------------------------------------------
// Create temporary files on disk so the rule can resolve imports and check
// for 'use client' directives via fs.readFileSync.

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'eslint-test-'));

// Create a 'use client' module with various exports
const clientModulePath = path.join(tmpDir, 'ClientComponent.tsx');
fs.writeFileSync(
  clientModulePath,
  `'use client';\nexport function ClientComponent() { return null; }\nexport function formatDate() {}\nexport const SOME_CONSTANT = 42;\n`,
);

// Create a 'use client' module with double-quoted directive
const doubleQuotedPath = path.join(tmpDir, 'DoubleQuoted.tsx');
fs.writeFileSync(
  doubleQuotedPath,
  `"use client";\nexport function DoubleQuotedComponent() { return null; }\nexport function helperFn() {}\n`,
);

// Create a shared module without 'use client'
const sharedModulePath = path.join(tmpDir, 'shared-utils.ts');
fs.writeFileSync(
  sharedModulePath,
  `export function formatLabel() {}\nexport const SHARED_CONST = 1;\n`,
);

// Create a server component file (no 'use client')
const serverPagePath = path.join(tmpDir, 'page.tsx');
fs.writeFileSync(serverPagePath, '// server component\n');

// Create a client component file
const anotherClientPath = path.join(tmpDir, 'AnotherClient.tsx');
fs.writeFileSync(
  anotherClientPath,
  `'use client';\nimport { formatDate } from './ClientComponent';\n`,
);

// ---------------------------------------------------------------------------
// Helper: lint code with the rule using ESLint's Linter API
// ---------------------------------------------------------------------------

function lintCode(code: string, filename: string): Linter.LintMessage[] {
  const linter = new Linter();
  linter.defineRule('no-client-utility-import', rule);
  return linter.verify(
    code,
    {
      parserOptions: { ecmaVersion: 2020, sourceType: 'module' },
      rules: { 'no-client-utility-import': 'warn' },
    },
    { filename },
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('no-client-utility-import', () => {
  afterAll(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  // --- Valid cases (no warnings) ---

  it('allows PascalCase named import from use client module', () => {
    const messages = lintCode(
      `import { ClientComponent } from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows default import from use client module', () => {
    const messages = lintCode(
      `import ClientComponent from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows any import from non-client shared module', () => {
    const messages = lintCode(
      `import { formatLabel, SHARED_CONST } from './shared-utils';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows package imports (not relative)', () => {
    const messages = lintCode(
      `import { useState } from 'react';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows client-to-client imports', () => {
    const messages = lintCode(
      `import { formatDate } from './ClientComponent';`,
      anotherClientPath,
    );
    expect(messages).toHaveLength(0);
  });

  it('allows PascalCase import from double-quoted use client module', () => {
    const messages = lintCode(
      `import { DoubleQuotedComponent } from './DoubleQuoted';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  it('does not crash on unresolvable imports', () => {
    const messages = lintCode(
      `import { something } from './does-not-exist';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(0);
  });

  // --- Invalid cases (should produce warnings) ---

  it('warns on camelCase named import from use client module', () => {
    const messages = lintCode(
      `import { formatDate } from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('formatDate');
    expect(messages[0].message).toContain("'use client'");
  });

  it('warns on ALL_CAPS constant import from use client module', () => {
    const messages = lintCode(
      `import { SOME_CONSTANT } from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('SOME_CONSTANT');
  });

  it('warns only on utility import in mixed import (component + utility)', () => {
    const messages = lintCode(
      `import { ClientComponent, formatDate } from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('formatDate');
  });

  it('warns on namespace import from use client module', () => {
    const messages = lintCode(
      `import * as Client from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('* as Client');
  });

  it('warns on utility import from double-quoted use client module', () => {
    const messages = lintCode(
      `import { helperFn } from './DoubleQuoted';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('helperFn');
  });

  it('warns on multiple utility imports from use client module', () => {
    const messages = lintCode(
      `import { formatDate, SOME_CONSTANT } from './ClientComponent';`,
      serverPagePath,
    );
    expect(messages).toHaveLength(2);
  });
});
