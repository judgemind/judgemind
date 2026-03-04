import type { Pool } from 'pg';
import type { FastifyReply } from 'fastify';
import { z } from 'zod';
import { GraphQLError } from 'graphql';
import {
  hashPassword,
  verifyPassword,
  signAccessToken,
  signVerificationToken,
  verifyVerificationToken,
  generateRefreshToken,
  hashRefreshToken,
  checkLoginRateLimit,
} from '../auth';
import type { AuthUser } from '../auth';
import { sendEmail, renderVerificationEmail } from '../email';

type Row = Record<string, unknown>;

interface AuthContext {
  pool: Pool;
  user: AuthUser | null;
  ip: string;
  reply: FastifyReply;
  cookieHeader: string;
}

const registerSchema = z.object({
  email: z.string().email().max(255),
  password: z.string().min(8).max(128),
  displayName: z.string().max(100).optional(),
});

const loginSchema = z.object({
  email: z.string().email(),
  password: z.string(),
});

const APP_URL = process.env.APP_URL ?? 'http://localhost:3000';
const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID ?? '';
const GOOGLE_CLIENT_SECRET = process.env.GOOGLE_CLIENT_SECRET ?? '';
const GOOGLE_REDIRECT_URI = process.env.GOOGLE_REDIRECT_URI ?? `${APP_URL}/auth/google/callback`;

function userRow(row: Row) {
  return {
    id: row.id,
    email: row.email,
    emailVerified: row.email_verified,
    displayName: row.display_name,
    role: row.role,
    createdAt: row.created_at,
  };
}

async function setRefreshTokenCookie(
  pool: Pool,
  userId: string,
  reply: FastifyReply,
): Promise<void> {
  const { token, hash, expiresAt } = generateRefreshToken();
  await pool.query(
    'INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3)',
    [userId, hash, expiresAt.toISOString()],
  );
  reply.header(
    'set-cookie',
    `refreshToken=${token}; HttpOnly; Secure; SameSite=Strict; Path=/graphql; Max-Age=${30 * 24 * 60 * 60}`,
  );
}

export const authResolvers = {
  Query: {
    me: (_: unknown, __: unknown, { user, pool }: AuthContext) => {
      if (!user) return null;
      return pool
        .query<Row>('SELECT * FROM users WHERE id = $1', [user.id])
        .then(({ rows }) => (rows[0] ? userRow(rows[0]) : null));
    },
  },

  Mutation: {
    register: async (
      _: unknown,
      args: { email: string; password: string; displayName?: string },
      { pool, reply }: AuthContext,
    ) => {
      const parsed = registerSchema.safeParse(args);
      if (!parsed.success) {
        throw new GraphQLError(parsed.error.issues[0].message, {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }
      const { email, password, displayName } = parsed.data;

      // Check for existing user
      const { rows: existing } = await pool.query<Row>(
        'SELECT id FROM users WHERE email = $1',
        [email],
      );
      if (existing.length > 0) {
        throw new GraphQLError('An account with this email already exists', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      const passwordHash = await hashPassword(password);
      const { rows } = await pool.query<Row>(
        `INSERT INTO users (email, password_hash, display_name, email_verified)
         VALUES ($1, $2, $3, false)
         RETURNING *`,
        [email, passwordHash, displayName ?? null],
      );
      const user = rows[0];

      // Send verification email (fire-and-forget: don't block auth flow)
      const verifyToken = signVerificationToken(user.id as string);
      const verificationUrl = `${APP_URL}/verify-email?token=${verifyToken}`;
      const { subject, html, text } = renderVerificationEmail({
        verificationUrl,
        displayName: displayName ?? undefined,
      });
      sendEmail({ to: email, subject, htmlBody: html, textBody: text }).catch(() => {
        // Log failure but don't break registration
      });

      const accessToken = signAccessToken({
        sub: user.id as string,
        email: user.email as string,
        role: user.role as string,
      });

      await setRefreshTokenCookie(pool, user.id as string, reply);

      return { accessToken, user: userRow(user) };
    },

    login: async (
      _: unknown,
      args: { email: string; password: string },
      { pool, reply, ip }: AuthContext,
    ) => {
      const parsed = loginSchema.safeParse(args);
      if (!parsed.success) {
        throw new GraphQLError(parsed.error.issues[0].message, {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      // Rate limiting
      const allowed = await checkLoginRateLimit(ip);
      if (!allowed) {
        throw new GraphQLError('Too many login attempts. Please try again later.', {
          extensions: { code: 'RATE_LIMITED' },
        });
      }

      const { email, password } = parsed.data;
      const { rows } = await pool.query<Row>(
        'SELECT * FROM users WHERE email = $1 AND is_active = true',
        [email],
      );
      const user = rows[0];

      if (!user || !user.password_hash) {
        throw new GraphQLError('Invalid email or password', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      const valid = await verifyPassword(password, user.password_hash as string);
      if (!valid) {
        throw new GraphQLError('Invalid email or password', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      // Update last login
      await pool.query('UPDATE users SET last_login_at = NOW() WHERE id = $1', [user.id]);

      const accessToken = signAccessToken({
        sub: user.id as string,
        email: user.email as string,
        role: user.role as string,
      });

      await setRefreshTokenCookie(pool, user.id as string, reply);

      return { accessToken, user: userRow(user) };
    },

    logout: async (_: unknown, __: unknown, { user, pool, reply }: AuthContext) => {
      if (!user) {
        throw new GraphQLError('Not authenticated', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      // Delete all refresh tokens for this user
      await pool.query('DELETE FROM refresh_tokens WHERE user_id = $1', [user.id]);

      // Clear cookie
      reply.header(
        'set-cookie',
        'refreshToken=; HttpOnly; Secure; SameSite=Strict; Path=/graphql; Max-Age=0',
      );

      return true;
    },

    refreshToken: async (
      _: unknown,
      __: unknown,
      { pool, reply, cookieHeader }: AuthContext,
    ) => {
      // Parse refresh token from cookie header
      const match = cookieHeader.match(/(?:^|;\s*)refreshToken=([^;]+)/);
      if (!match) {
        throw new GraphQLError('No refresh token', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      const rawToken = match[1];
      const tokenHash = hashRefreshToken(rawToken);

      const { rows: tokenRows } = await pool.query<Row>(
        'SELECT * FROM refresh_tokens WHERE token_hash = $1 AND expires_at > NOW()',
        [tokenHash],
      );
      if (tokenRows.length === 0) {
        throw new GraphQLError('Invalid or expired refresh token', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      const refreshRow = tokenRows[0];
      const userId = refreshRow.user_id as string;

      // Delete the used token (rotation)
      await pool.query('DELETE FROM refresh_tokens WHERE id = $1', [refreshRow.id]);

      const { rows: userRows } = await pool.query<Row>(
        'SELECT * FROM users WHERE id = $1 AND is_active = true',
        [userId],
      );
      if (userRows.length === 0) {
        throw new GraphQLError('User not found', {
          extensions: { code: 'UNAUTHENTICATED' },
        });
      }

      const user = userRows[0];
      const accessToken = signAccessToken({
        sub: user.id as string,
        email: user.email as string,
        role: user.role as string,
      });

      await setRefreshTokenCookie(pool, user.id as string, reply);

      return { accessToken, user: userRow(user) };
    },

    verifyEmail: async (_: unknown, { token }: { token: string }, { pool }: AuthContext) => {
      try {
        const payload = verifyVerificationToken(token);
        const { rowCount } = await pool.query(
          'UPDATE users SET email_verified = true WHERE id = $1 AND email_verified = false',
          [payload.sub],
        );
        return (rowCount ?? 0) > 0;
      } catch {
        throw new GraphQLError('Invalid or expired verification token', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }
    },

    initiateGoogleAuth: () => {
      if (!GOOGLE_CLIENT_ID) {
        throw new GraphQLError('Google OAuth is not configured', {
          extensions: { code: 'INTERNAL_SERVER_ERROR' },
        });
      }
      const params = new URLSearchParams({
        client_id: GOOGLE_CLIENT_ID,
        redirect_uri: GOOGLE_REDIRECT_URI,
        response_type: 'code',
        scope: 'openid email profile',
        access_type: 'offline',
        prompt: 'consent',
      });
      return `https://accounts.google.com/o/oauth2/v2/auth?${params}`;
    },

    completeGoogleAuth: async (
      _: unknown,
      { code }: { code: string },
      { pool, reply }: AuthContext,
    ) => {
      if (!GOOGLE_CLIENT_ID || !GOOGLE_CLIENT_SECRET) {
        throw new GraphQLError('Google OAuth is not configured', {
          extensions: { code: 'INTERNAL_SERVER_ERROR' },
        });
      }

      // Exchange code for tokens
      const tokenRes = await fetch('https://oauth2.googleapis.com/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          code,
          client_id: GOOGLE_CLIENT_ID,
          client_secret: GOOGLE_CLIENT_SECRET,
          redirect_uri: GOOGLE_REDIRECT_URI,
          grant_type: 'authorization_code',
        }),
      });

      if (!tokenRes.ok) {
        throw new GraphQLError('Failed to exchange Google authorization code', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      const tokenData = (await tokenRes.json()) as { id_token?: string };
      if (!tokenData.id_token) {
        throw new GraphQLError('No ID token received from Google', {
          extensions: { code: 'INTERNAL_SERVER_ERROR' },
        });
      }

      // Decode ID token (Google's id_token is a JWT — verify via Google's public keys in production)
      const [, payloadB64] = tokenData.id_token.split('.');
      const googlePayload = JSON.parse(Buffer.from(payloadB64, 'base64').toString()) as {
        sub: string;
        email: string;
        email_verified?: boolean;
        name?: string;
      };

      const { sub: googleId, email, name } = googlePayload;

      // Look up existing user by google_id or email
      const { rows: existing } = await pool.query<Row>(
        'SELECT * FROM users WHERE google_id = $1 OR email = $2 LIMIT 1',
        [googleId, email],
      );

      let user: Row;
      if (existing.length > 0) {
        user = existing[0];
        // Link google_id if not set
        if (!user.google_id) {
          await pool.query('UPDATE users SET google_id = $1 WHERE id = $2', [googleId, user.id]);
        }
        // Mark email verified via Google
        if (!user.email_verified) {
          await pool.query('UPDATE users SET email_verified = true WHERE id = $1', [user.id]);
          user.email_verified = true;
        }
        await pool.query('UPDATE users SET last_login_at = NOW() WHERE id = $1', [user.id]);
      } else {
        const { rows } = await pool.query<Row>(
          `INSERT INTO users (email, google_id, display_name, email_verified)
           VALUES ($1, $2, $3, true)
           RETURNING *`,
          [email, googleId, name ?? null],
        );
        user = rows[0];
      }

      const accessToken = signAccessToken({
        sub: user.id as string,
        email: user.email as string,
        role: user.role as string,
      });

      await setRefreshTokenCookie(pool, user.id as string, reply);

      return { accessToken, user: userRow(user) };
    },
  },

  User: {
    emailVerified: (row: Row) => row.email_verified ?? row.emailVerified,
    displayName: (row: Row) => row.display_name ?? row.displayName,
    createdAt: (row: Row) => row.created_at ?? row.createdAt,
  },
};
