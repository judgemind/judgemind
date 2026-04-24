import { describe, it, expect } from 'vitest';
import { Linter } from 'eslint';

// Load the rule
// eslint-disable-next-line @typescript-eslint/no-var-requires
const rule = require('../../eslint-rules/polled-query-error-guard');

// ---------------------------------------------------------------------------
// Helper: lint JSX code with the rule using ESLint's Linter API
//
// The default espree parser supports JSX when ecmaFeatures.jsx is enabled.
// We do not need @typescript-eslint/parser for the JS/JSX-level constructs
// this rule inspects (destructuring pattern, IfStatement test, JSX returns).
// ---------------------------------------------------------------------------

function lintCode(code: string): Linter.LintMessage[] {
  const linter = new Linter();
  linter.defineRule('polled-query-error-guard', rule);
  return linter.verify(
    code,
    {
      parserOptions: {
        ecmaVersion: 2020,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
      rules: { 'polled-query-error-guard': 'error' },
    },
    { filename: '/fake/src/app/admin/test-component.tsx' },
  );
}

// Wrap a component snippet so the parser can handle it.
// Uses plain JS/JSX syntax (no TypeScript annotations) because the Linter
// is configured with espree + JSX — not @typescript-eslint/parser.
function wrap(inner: string): string {
  return [
    `import React from 'react';`,
    ``,
    `function ErrorBanner(props) {`,
    `  return <div>Error</div>;`,
    `}`,
    ``,
    `export function Component() {`,
    `${inner}`,
    `}`,
  ].join('\n');
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('polled-query-error-guard', () => {
  // --- Valid cases (no errors expected) ---

  it('allows useQuery without pollInterval', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY);
  if (error) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows useQuery with pollInterval and guarded if (error && !data)', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error && !data) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows renamed bindings with proper guard', () => {
    const messages = lintCode(
      wrap(`
  const { data: state, loading, error: queryError } = useQuery(QUERY, { pollInterval: 5000 });
  if (queryError && !state) return <ErrorBanner error={queryError} />;
  return <div>{state}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows if (error || loading) — logical expression is already guarded', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error || loading) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows polled useQuery when error is not destructured', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading } = useQuery(QUERY, { pollInterval: 5000 });
  if (loading) return <div>Loading...</div>;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows useQuery with pollInterval: 0 but error guard still valid', () => {
    // pollInterval: 0 disables polling but we only care about whether it's
    // present in the options object — the static check cannot evaluate it.
    // If options has pollInterval key the rule fires, but if the consequent
    // is already guarded it passes.
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 0 });
  if (error && !data) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  it('allows if (error) when consequent does not return JSX', () => {
    // Bare if (error) that does something other than return JSX should not fire.
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error) console.error(error);
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });

  // --- Invalid cases (should produce errors) ---

  it('reports error for bare if (error) with JSX return under polled query', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('error');
    expect(messages[0].message).toContain('data');
    expect(messages[0].message).toContain('docs/web-patterns.md');
    expect(messages[0].message).toContain('2850');
    expect(messages[0].severity).toBe(2); // error
  });

  it('reports error for bare if (error) with JSX in block statement', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error) {
    return <ErrorBanner error={error} />;
  }
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('error');
  });

  it('reports error for renamed error binding', () => {
    const messages = lintCode(
      wrap(`
  const { data: state, error: queryError } = useQuery(QUERY, { pollInterval: 5000 });
  if (queryError) return <ErrorBanner error={queryError} />;
  return <div>{state}</div>;
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('queryError');
    expect(messages[0].message).toContain('state');
  });

  it('error message references docs/web-patterns.md §Apollo Polling', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
  if (error) return <ErrorBanner error={error} />;
  return <div>{data}</div>;
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('docs/web-patterns.md');
    expect(messages[0].message).toContain('Apollo Polling');
    expect(messages[0].message).toContain('2850');
  });

  // ---------------------------------------------------------------------------
  // Regression fixtures: pre-#2842 vs post-#2842 DispatcherDashboard shape
  //
  // Before #2842 the DispatcherDashboard used:
  //   const { data, loading, error, refetch } = useQuery(..., { pollInterval: ... });
  //   if (error) return <ErrorBanner error={error} />;
  //
  // After #2842 it was fixed to:
  //   const { data: state, loading, error, refetch } = useQuery(..., ...);
  //   if (error && !state) return <ErrorBanner error={error} />;
  // ---------------------------------------------------------------------------

  it('fires on pre-#2842 DispatcherDashboard shape (polled query + bare if error)', () => {
    const messages = lintCode(
      wrap(`
  const { data, loading, error, refetch } = useQuery(DISPATCHER_STATE_QUERY, {
    skip: !authReady,
    pollInterval: authReady ? 2000 : 0,
    fetchPolicy: 'cache-and-network',
  });
  if (error) return <ErrorBanner error={error} />;
  return <div>{data ? 'ok' : 'loading'}</div>;
      `),
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toContain('error');
    expect(messages[0].message).toContain('data');
  });

  it('passes on post-#2842 DispatcherDashboard shape (guarded with error && !state)', () => {
    const messages = lintCode(
      wrap(`
  const { data: state, loading, error, refetch } = useQuery(DISPATCHER_STATE_QUERY, {
    skip: !authReady,
    pollInterval: authReady ? 2000 : 0,
    fetchPolicy: 'cache-and-network',
  });
  if (error && !state) return <ErrorBanner error={error} />;
  return <div>{state ? 'ok' : 'loading'}</div>;
      `),
    );
    expect(messages).toHaveLength(0);
  });
});
