import { describe, it, expect } from 'vitest';
import { hashPassword, verifyPassword } from '../src/auth/passwords';

describe('hashPassword', () => {
  it('returns a bcrypt hash string', async () => {
    const hash = await hashPassword('my-secret-password');
    // bcrypt hashes start with $2a$ or $2b$
    expect(hash).toMatch(/^\$2[ab]\$/);
  });

  it('produces different hashes for the same password (salt differs)', async () => {
    const hash1 = await hashPassword('same-password');
    const hash2 = await hashPassword('same-password');
    expect(hash1).not.toBe(hash2);
  });
});

describe('verifyPassword', () => {
  it('returns true for the correct password', async () => {
    const password = 'correct-horse-battery-staple';
    const hash = await hashPassword(password);
    const result = await verifyPassword(password, hash);
    expect(result).toBe(true);
  });

  it('returns false for an incorrect password', async () => {
    const hash = await hashPassword('real-password');
    const result = await verifyPassword('wrong-password', hash);
    expect(result).toBe(false);
  });

  it('returns false for an empty password against a valid hash', async () => {
    const hash = await hashPassword('non-empty');
    const result = await verifyPassword('', hash);
    expect(result).toBe(false);
  });
});
