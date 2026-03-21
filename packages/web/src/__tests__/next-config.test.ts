import { describe, it, expect } from 'vitest';
import path from 'path';

/**
 * Regression test for the Next.js config.
 *
 * isomorphic-dompurify must be listed in serverComponentsExternalPackages
 * so that Next.js resolves it at runtime (from node_modules) instead of
 * bundling it with webpack. Without this, jsdom's native dependencies
 * cause HTTP 500 errors during SSR of client components that call
 * sanitizeRulingHtml or sanitizeExcerptHtml.
 *
 * See: https://github.com/judgemind/judgemind/issues/1280
 */
describe('next.config.js', () => {
  it('externalises isomorphic-dompurify for server-side rendering', () => {
    const configPath = path.resolve(__dirname, '../../next.config.js');
    const nextConfig = require(configPath);
    const externals =
      nextConfig.experimental?.serverComponentsExternalPackages ?? [];
    expect(externals).toContain('isomorphic-dompurify');
  });
});
