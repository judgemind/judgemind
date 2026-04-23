import jwt from 'jsonwebtoken';
import crypto from 'crypto';

const JWT_SECRET = process.env.JWT_SECRET ?? 'dev-jwt-secret-change-in-production';
const ACCESS_TOKEN_EXPIRY = '15m';
const REFRESH_TOKEN_EXPIRY_DAYS = 30;

export interface AccessTokenPayload {
  sub: string; // user id
  email: string;
  role: string;
}

export interface VerificationTokenPayload {
  sub: string; // user id
  purpose: 'email-verification';
}

export function signAccessToken(payload: AccessTokenPayload): string {
  return jwt.sign(payload, JWT_SECRET, { expiresIn: ACCESS_TOKEN_EXPIRY });
}

export function verifyAccessToken(token: string): AccessTokenPayload {
  return jwt.verify(token, JWT_SECRET) as AccessTokenPayload;
}

export function signVerificationToken(userId: string): string {
  const payload: VerificationTokenPayload = { sub: userId, purpose: 'email-verification' };
  return jwt.sign(payload, JWT_SECRET, { expiresIn: '24h' });
}

export function verifyVerificationToken(token: string): VerificationTokenPayload {
  const payload = jwt.verify(token, JWT_SECRET) as VerificationTokenPayload;
  if (payload.purpose !== 'email-verification') {
    throw new Error('Invalid token purpose');
  }
  return payload;
}

/** Generate a cryptographically random refresh token and its SHA-256 hash for DB storage. */
export function generateRefreshToken(): { token: string; hash: string; expiresAt: Date } {
  const token = crypto.randomBytes(48).toString('base64url');
  const hash = crypto.createHash('sha256').update(token).digest('hex');
  const expiresAt = new Date(Date.now() + REFRESH_TOKEN_EXPIRY_DAYS * 24 * 60 * 60 * 1000);
  return { token, hash, expiresAt };
}

/** Hash a raw refresh token for comparison against the DB. */
export function hashRefreshToken(token: string): string {
  return crypto.createHash('sha256').update(token).digest('hex');
}
