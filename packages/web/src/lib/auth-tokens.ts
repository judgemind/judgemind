/**
 * In-memory access token store.
 *
 * The access token is a short-lived JWT (15 min). Storing it in a module
 * variable keeps it out of localStorage (XSS-safe) while still being
 * accessible across the Apollo link chain.
 *
 * On page refresh the token is lost, which is expected — the Apollo client
 * will use the `refreshToken` mutation (sent with the HTTP-only cookie) to
 * obtain a fresh access token automatically.
 */

let accessToken: string | null = null;

/** Return the current in-memory access token, or null. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** Store a new access token in memory. Called after login, register, or token refresh. */
export function setAccessToken(token: string | null): void {
  accessToken = token;
}

/** Clear the in-memory access token. Called on logout. */
export function clearAccessToken(): void {
  accessToken = null;
}
