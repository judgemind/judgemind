/**
 * Unit tests for JWT_SECRET startup guard in tokens.ts.
 *
 * Because tokens.ts reads JWT_SECRET at module load time, each scenario
 * resets the module registry and dynamically imports the module under the
 * desired environment variables.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('tokens — JWT_SECRET guard', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
    vi.resetModules();
  });

  afterEach(() => {
    process.env = originalEnv;
    vi.resetModules();
  });

  it('throws on import when NODE_ENV=production and JWT_SECRET is missing', async () => {
    process.env.NODE_ENV = 'production';
    delete process.env.JWT_SECRET;

    await expect(import('../src/auth/tokens')).rejects.toThrow(
      'JWT_SECRET environment variable must be set in production',
    );
  });

  it('does not throw on import when NODE_ENV=production and JWT_SECRET is set', async () => {
    process.env.NODE_ENV = 'production';
    process.env.JWT_SECRET = 'a-very-strong-production-secret-32chars';

    await expect(import('../src/auth/tokens')).resolves.toBeDefined();
  });

  it('does not throw in development when JWT_SECRET is missing', async () => {
    process.env.NODE_ENV = 'development';
    delete process.env.JWT_SECRET;

    await expect(import('../src/auth/tokens')).resolves.toBeDefined();
  });

  it('does not throw in test environment when JWT_SECRET is missing', async () => {
    process.env.NODE_ENV = 'test';
    delete process.env.JWT_SECRET;

    await expect(import('../src/auth/tokens')).resolves.toBeDefined();
  });
});
