import { describe, it, expect } from 'vitest';
import { Linter } from 'eslint';

// Load the rule
// eslint-disable-next-line @typescript-eslint/no-var-requires
const rule = require('../../eslint-rules/clickable-table-row');

// ---------------------------------------------------------------------------
// Helper: lint JSX code with the rule using ESLint's Linter API
//
// The default espree parser supports JSX when ecmaFeatures.jsx is enabled.
// We do not need @typescript-eslint/parser for the JS/JSX-level constructs
// this rule inspects (opening element names, attribute names, JSX children).
// ---------------------------------------------------------------------------

function lintCode(code: string): Linter.LintMessage[] {
  const linter = new Linter();
  linter.defineRule('clickable-table-row', rule);
  return linter.verify(
    code,
    {
      parserOptions: {
        ecmaVersion: 2020,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
      rules: { 'clickable-table-row': 'error' },
    },
    { filename: '/fake/src/app/rulings/page.tsx' },
  );
}

// Wrap a JSX snippet in a minimal component so the parser can handle it.
function wrap(jsx: string): string {
  return [
    `import React from 'react';`,
    `import Link from 'next/link';`,
    `import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';`,
    ``,
    `export function Component() {`,
    `  return (`,
    `    ${jsx}`,
    `  );`,
    `}`,
  ].join('\n');
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('clickable-table-row', () => {
  // --- Valid cases (no errors) ---

  it('allows a TableRow with onClick and a nested Link', () => {
    const messages = lintCode(
      wrap(`
        <TableRow onClick={() => {}}>
          <TableCell>
            <Link href="/foo" onClick={(e) => e.stopPropagation()}>Name</Link>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows a TableRow without any Link (plain data row)', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>Plain text</TableCell>
          <TableCell>More text</TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows a header TableRow containing Link inside TableHead', () => {
    const messages = lintCode(
      wrap(`
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>
                <Link href="/judges/1">Judge Name</Link>
              </TableHead>
              <TableHead>Metric</TableHead>
            </TableRow>
          </TableHeader>
        </Table>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows a TableRow with a spread attribute (may provide onClick at runtime)', () => {
    // Spread attributes cannot be statically analyzed for onClick — we treat
    // them as potentially providing the handler to avoid false positives.
    const messages = lintCode(
      wrap(`
        <TableRow {...rowProps}>
          <TableCell>
            <Link href="/foo">Name</Link>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows multiple TableRows with the correct pattern', () => {
    const messages = lintCode(
      wrap(`
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow onClick={() => {}}>
              <TableCell>
                <Link href="/foo" onClick={(e) => e.stopPropagation()}>Alice</Link>
              </TableCell>
            </TableRow>
            <TableRow onClick={() => {}}>
              <TableCell>
                <Link href="/bar" onClick={(e) => e.stopPropagation()}>Bob</Link>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  // --- Invalid cases (should produce errors) ---

  it('reports error for TableRow with nested Link but no onClick', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <Link href="/foo">Name</Link>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('onClick');
    expect(messages[0].message).toContain('<TableRow>');
    expect(messages[0].message).toContain('Link');
    expect(messages[0].message).toContain('2156');
    expect(messages[0].severity).toBe(2); // error
  });

  it('reports error for TableRow with nested <a> tag but no onClick', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <a href="/foo">Name</a>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('onClick');
  });

  it('reports error for TableRow with deeply nested Link (inside a div inside TableCell)', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <div className="wrapper">
              <span><Link href="/foo">Name</Link></span>
            </div>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('onClick');
  });

  it('reports error for TableRow rendered inside a map without onClick', () => {
    const messages = lintCode(
      wrap(`
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.id}>
              <TableCell>
                <Link href={\`/foo/\${item.id}\`}>{item.name}</Link>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('onClick');
  });

  it('reports only one error per offending row, not per nested link', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <Link href="/foo">Primary</Link>
            <Link href="/bar">Secondary</Link>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
  });

  it('reports error for each offending row independently', () => {
    const messages = lintCode(
      wrap(`
        <TableBody>
          <TableRow>
            <TableCell>
              <Link href="/foo">Alice</Link>
            </TableCell>
          </TableRow>
          <TableRow onClick={() => {}}>
            <TableCell>
              <Link href="/bar" onClick={(e) => e.stopPropagation()}>Bob</Link>
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>
              <Link href="/baz">Charlie</Link>
            </TableCell>
          </TableRow>
        </TableBody>
      `),
    );
    expect(messages).toHaveLength(2);
  });

  it('does not flag the same link twice when a parent TableRow was already flagged', () => {
    // A Link nested inside a TableRow should attribute to the outer row only.
    // This is the same as the "one error per row" case above, but verifies
    // the rule does not double-count by separately considering each Link.
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <div>
              <Link href="/a">A</Link>
              <Link href="/b">B</Link>
              <Link href="/c">C</Link>
            </div>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
  });

  it('error message references docs/web-patterns.md', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableCell>
            <Link href="/foo">Name</Link>
          </TableCell>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('docs/web-patterns.md');
    expect(messages[0].message).toContain('Clickable Table Rows');
  });

  it('does not flag a TableRow that only contains TableHead children (header row)', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableHead>
            <Link href="/judges/1">Judge 1</Link>
          </TableHead>
          <TableHead>
            <Link href="/judges/2">Judge 2</Link>
          </TableHead>
        </TableRow>
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('flags a TableRow with a mix of TableCell and Link (not a header row)', () => {
    const messages = lintCode(
      wrap(`
        <TableRow>
          <TableHead>Label</TableHead>
          <TableCell>
            <Link href="/foo">Value</Link>
          </TableCell>
        </TableRow>
      `),
    );
    // Mixed rows are treated as data rows (because they contain TableCell),
    // so the nested Link triggers the rule.
    expect(messages).toHaveLength(1);
  });
});
