import { describe, it, expect, beforeEach, vi } from 'vitest';

// Reset modules between tests to get fresh module state
beforeEach(() => {
  vi.resetModules();
});

describe('auth-tokens', () => {
  it('getAccessToken returns null by default', async () => {
    const { getAccessToken } = await import('../auth-tokens');
    expect(getAccessToken()).toBeNull();
  });

  it('setAccessToken stores a token that getAccessToken returns', async () => {
    const { getAccessToken, setAccessToken } = await import('../auth-tokens');
    setAccessToken('test-jwt-token');
    expect(getAccessToken()).toBe('test-jwt-token');
  });

  it('setAccessToken(null) clears the token', async () => {
    const { getAccessToken, setAccessToken } = await import('../auth-tokens');
    setAccessToken('some-token');
    setAccessToken(null);
    expect(getAccessToken()).toBeNull();
  });

  it('clearAccessToken resets the token to null', async () => {
    const { getAccessToken, setAccessToken, clearAccessToken } = await import(
      '../auth-tokens'
    );
    setAccessToken('another-token');
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
  });
});
