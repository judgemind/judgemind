import { describe, it, expect } from 'vitest';
import {
  LOGIN_MUTATION,
  REGISTER_MUTATION,
  VERIFY_EMAIL_MUTATION,
  LOGOUT_MUTATION,
  ME_QUERY,
} from '../src/lib/auth-mutations';
import type { AuthUser, AuthPayload } from '../src/lib/auth-mutations';

describe('auth-mutations', () => {
  it('LOGIN_MUTATION is a valid DocumentNode', () => {
    expect(LOGIN_MUTATION).toBeDefined();
    expect(LOGIN_MUTATION.kind).toBe('Document');
  });

  it('REGISTER_MUTATION is a valid DocumentNode', () => {
    expect(REGISTER_MUTATION).toBeDefined();
    expect(REGISTER_MUTATION.kind).toBe('Document');
  });

  it('VERIFY_EMAIL_MUTATION is a valid DocumentNode', () => {
    expect(VERIFY_EMAIL_MUTATION).toBeDefined();
    expect(VERIFY_EMAIL_MUTATION.kind).toBe('Document');
  });

  it('LOGOUT_MUTATION is a valid DocumentNode', () => {
    expect(LOGOUT_MUTATION).toBeDefined();
    expect(LOGOUT_MUTATION.kind).toBe('Document');
  });

  it('ME_QUERY is a valid DocumentNode', () => {
    expect(ME_QUERY).toBeDefined();
    expect(ME_QUERY.kind).toBe('Document');
  });

  it('AuthUser type is structurally valid at compile time', () => {
    // This is a compile-time check — if the interface changes, this test
    // will fail to compile, catching regressions.
    const user: AuthUser = {
      id: '1',
      email: 'test@example.com',
      emailVerified: false,
      displayName: null,
      role: 'user',
      createdAt: '2026-01-01T00:00:00Z',
    };
    expect(user.id).toBe('1');
    expect(user.email).toBe('test@example.com');
    expect(user.emailVerified).toBe(false);
  });

  it('AuthPayload type includes accessToken and user', () => {
    const payload: AuthPayload = {
      accessToken: 'jwt-token',
      user: {
        id: '1',
        email: 'test@example.com',
        emailVerified: true,
        displayName: 'Test User',
        role: 'user',
        createdAt: '2026-01-01T00:00:00Z',
      },
    };
    expect(payload.accessToken).toBe('jwt-token');
    expect(payload.user.displayName).toBe('Test User');
  });

  it('LOGIN_MUTATION requests all user fields', () => {
    // Verify the query string includes the expected fields
    const source = LOGIN_MUTATION.loc?.source?.body ?? '';
    expect(source).toContain('email');
    expect(source).toContain('password');
    expect(source).toContain('accessToken');
    expect(source).toContain('emailVerified');
    expect(source).toContain('displayName');
  });

  it('REGISTER_MUTATION accepts optional displayName', () => {
    const source = REGISTER_MUTATION.loc?.source?.body ?? '';
    expect(source).toContain('$displayName: String');
    expect(source).toContain('displayName: $displayName');
  });
});
