import { describe, it, expect, vi } from 'vitest';
import jwt from 'jsonwebtoken';
import {
  signAccessToken,
  verifyAccessToken,
  signVerificationToken,
  verifyVerificationToken,
  generateRefreshToken,
  hashRefreshToken,
} from '../src/auth/tokens';
import type { AccessTokenPayload } from '../src/auth/tokens';

describe('signAccessToken / verifyAccessToken', () => {
  it('round-trips a valid payload', () => {
    const payload: AccessTokenPayload = {
      sub: 'user-123',
      email: 'alice@example.com',
      role: 'user',
    };
    const token = signAccessToken(payload);
    const decoded = verifyAccessToken(token);

    expect(decoded.sub).toBe('user-123');
    expect(decoded.email).toBe('alice@example.com');
    expect(decoded.role).toBe('user');
  });

  it('throws on a malformed token', () => {
    expect(() => verifyAccessToken('not-a-real-jwt')).toThrow();
  });

  it('throws on a tampered token', () => {
    const token = signAccessToken({ sub: '1', email: 'a@b.com', role: 'user' });
    // Flip a character in the signature portion
    const parts = token.split('.');
    parts[2] = parts[2].slice(0, -1) + (parts[2].slice(-1) === 'A' ? 'B' : 'A');
    const tampered = parts.join('.');
    expect(() => verifyAccessToken(tampered)).toThrow();
  });

  it('throws on an expired token', () => {
    vi.useFakeTimers();
    const token = signAccessToken({ sub: '1', email: 'a@b.com', role: 'user' });
    // Advance 20 minutes (token expires in 15m)
    vi.advanceTimersByTime(20 * 60 * 1000);
    expect(() => verifyAccessToken(token)).toThrow();
    vi.useRealTimers();
  });
});

describe('signVerificationToken / verifyVerificationToken', () => {
  it('round-trips a valid verification token', () => {
    const token = signVerificationToken('user-456');
    const decoded = verifyVerificationToken(token);

    expect(decoded.sub).toBe('user-456');
    expect(decoded.purpose).toBe('email-verification');
  });

  it('rejects a token with wrong purpose', () => {
    const secret = process.env.JWT_SECRET ?? 'dev-jwt-secret-change-in-production';
    const badToken = jwt.sign({ sub: 'user-1', purpose: 'password-reset' }, secret, {
      expiresIn: '24h',
    });
    expect(() => verifyVerificationToken(badToken)).toThrow('Invalid token purpose');
  });

  it('rejects an expired verification token', () => {
    vi.useFakeTimers();
    const token = signVerificationToken('user-789');
    // Advance 25 hours (token expires in 24h)
    vi.advanceTimersByTime(25 * 60 * 60 * 1000);
    expect(() => verifyVerificationToken(token)).toThrow();
    vi.useRealTimers();
  });
});

describe('generateRefreshToken', () => {
  it('returns token, hash, and expiresAt', () => {
    const result = generateRefreshToken();

    expect(result.token).toBeDefined();
    expect(typeof result.token).toBe('string');
    expect(result.token.length).toBeGreaterThan(0);

    expect(result.hash).toBeDefined();
    expect(result.hash).toMatch(/^[0-9a-f]{64}$/); // SHA-256 hex

    expect(result.expiresAt).toBeInstanceOf(Date);
    expect(result.expiresAt.getTime()).toBeGreaterThan(Date.now());
  });

  it('generates unique tokens each call', () => {
    const r1 = generateRefreshToken();
    const r2 = generateRefreshToken();

    expect(r1.token).not.toBe(r2.token);
    expect(r1.hash).not.toBe(r2.hash);
  });

  it('sets expiry approximately 30 days from now', () => {
    const before = Date.now();
    const result = generateRefreshToken();
    const after = Date.now();

    const thirtyDaysMs = 30 * 24 * 60 * 60 * 1000;
    expect(result.expiresAt.getTime()).toBeGreaterThanOrEqual(before + thirtyDaysMs);
    expect(result.expiresAt.getTime()).toBeLessThanOrEqual(after + thirtyDaysMs);
  });
});

describe('hashRefreshToken', () => {
  it('produces a consistent SHA-256 hash for the same input', () => {
    const hash1 = hashRefreshToken('my-token');
    const hash2 = hashRefreshToken('my-token');

    expect(hash1).toBe(hash2);
    expect(hash1).toMatch(/^[0-9a-f]{64}$/);
  });

  it('matches the hash from generateRefreshToken', () => {
    const { token, hash } = generateRefreshToken();
    expect(hashRefreshToken(token)).toBe(hash);
  });
});
