/**
 * Thin GitHub REST client + in-memory TTL cache for the dispatcher admin
 * page (#2805). Used to enrich `dispatcher.queue_snapshots.issue_numbers`
 * with title/labels/created_at/priority and to fetch open blocked issues
 * that are NOT tracked in the snapshots (which only cover `agent/ready`).
 *
 * Rate-limit strategy:
 *   - Honour `process.env.GITHUB_TOKEN` when set (bumps rate to 5 000/hr).
 *   - Otherwise fall back to unauthenticated (60/hr) with aggressive caching.
 *   - Cache per-issue lookups for 30 s (matches the daemon's scheduler
 *     cadence — by the time an issue changes, the next snapshot-triggered
 *     refresh will see it).
 *   - Cache the `status/blocked` search for 30 s.
 *
 * Graceful degradation:
 *   - On any fetch/parse failure we return `null` (or an empty list for
 *     the search endpoint). Callers fill in a minimal `QueueItem`/
 *     `RecentCompletion` with just the issue number — the UI renders
 *     `#N — (title unavailable)` rather than failing the whole page.
 *
 * NOTE: intentionally uses the global `fetch` (Node 18+). No runtime
 * dependencies added.
 */

const DEFAULT_REPO = process.env.JUDGEMIND_GITHUB_REPO ?? 'judgemind/judgemind';
const API_BASE = 'https://api.github.com';

const ISSUE_CACHE_TTL_MS = 30 * 1000;
const SEARCH_CACHE_TTL_MS = 30 * 1000;

export interface GitHubIssue {
  number: number;
  title: string;
  labels: string[];
  createdAt: string;
  body: string | null;
}

interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

const issueCache = new Map<number, CacheEntry<GitHubIssue | null>>();
const searchCache = new Map<string, CacheEntry<GitHubIssue[]>>();

function now(): number {
  return Date.now();
}

function authHeaders(): Record<string, string> {
  const token = process.env.GITHUB_TOKEN ?? process.env.JUDGEMIND_GITHUB_TOKEN;
  const headers: Record<string, string> = {
    'user-agent': 'judgemind-api/dispatcher-admin',
    accept: 'application/vnd.github+json',
    'x-github-api-version': '2022-11-28',
  };
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  return headers;
}

/** Parse a GitHub Issue JSON payload into the reduced shape we use. */
function parseIssue(raw: unknown): GitHubIssue | null {
  if (!raw || typeof raw !== 'object') return null;
  const record = raw as Record<string, unknown>;
  const number = record.number;
  if (typeof number !== 'number') return null;
  const title = typeof record.title === 'string' ? record.title : '';
  const createdAt = typeof record.created_at === 'string' ? record.created_at : '';
  const body = typeof record.body === 'string' ? record.body : null;
  const labelsRaw = Array.isArray(record.labels) ? record.labels : [];
  const labels = labelsRaw
    .map((l) => {
      if (typeof l === 'string') return l;
      if (l && typeof l === 'object' && 'name' in l) {
        const name = (l as { name: unknown }).name;
        return typeof name === 'string' ? name : null;
      }
      return null;
    })
    .filter((l): l is string => typeof l === 'string');
  return { number, title, labels, createdAt, body };
}

/**
 * Fetch a single GitHub issue by number. Returns null on any failure.
 * Cached for 30 s per issue.
 */
export async function fetchIssue(
  issueNumber: number,
  repo: string = DEFAULT_REPO,
): Promise<GitHubIssue | null> {
  const cached = issueCache.get(issueNumber);
  if (cached && cached.expiresAt > now()) {
    return cached.value;
  }
  const url = `${API_BASE}/repos/${repo}/issues/${issueNumber}`;
  try {
    const response = await fetch(url, { headers: authHeaders() });
    if (!response.ok) {
      // 404 or rate-limit — cache null briefly so we don't hammer the API.
      issueCache.set(issueNumber, { value: null, expiresAt: now() + ISSUE_CACHE_TTL_MS });
      return null;
    }
    const json = await response.json();
    const parsed = parseIssue(json);
    issueCache.set(issueNumber, { value: parsed, expiresAt: now() + ISSUE_CACHE_TTL_MS });
    return parsed;
  } catch {
    issueCache.set(issueNumber, { value: null, expiresAt: now() + ISSUE_CACHE_TTL_MS });
    return null;
  }
}

/**
 * Fetch many GitHub issues concurrently. Simple batched Promise.all —
 * cache hits return synchronously. Caps at the caller-supplied `limit`.
 */
export async function fetchIssues(
  numbers: readonly number[],
  repo: string = DEFAULT_REPO,
): Promise<Array<GitHubIssue | null>> {
  return Promise.all(numbers.map((n) => fetchIssue(n, repo)));
}

/**
 * Search for open issues matching a query. Cached per-query for 30 s.
 * Returns an empty array on any failure.
 */
export async function searchIssues(
  query: string,
  repo: string = DEFAULT_REPO,
  limit = 10,
): Promise<GitHubIssue[]> {
  const cacheKey = `${repo}::${query}::${limit}`;
  const cached = searchCache.get(cacheKey);
  if (cached && cached.expiresAt > now()) {
    return cached.value;
  }
  const fullQuery = encodeURIComponent(`repo:${repo} is:open is:issue ${query}`);
  const url = `${API_BASE}/search/issues?q=${fullQuery}&per_page=${limit}`;
  try {
    const response = await fetch(url, { headers: authHeaders() });
    if (!response.ok) {
      searchCache.set(cacheKey, { value: [], expiresAt: now() + SEARCH_CACHE_TTL_MS });
      return [];
    }
    const json = (await response.json()) as { items?: unknown[] };
    const items = Array.isArray(json.items) ? json.items : [];
    const parsed = items
      .map((item) => parseIssue(item))
      .filter((x): x is GitHubIssue => x !== null);
    searchCache.set(cacheKey, { value: parsed, expiresAt: now() + SEARCH_CACHE_TTL_MS });
    return parsed;
  } catch {
    searchCache.set(cacheKey, { value: [], expiresAt: now() + SEARCH_CACHE_TTL_MS });
    return [];
  }
}

/**
 * Extract `Blocked by #N` numbers from an issue body. Matches the same
 * pattern the `unblock-issues` workflow uses.
 */
export function parseBlockedBy(body: string | null): number[] {
  if (!body) return [];
  const matches = body.matchAll(/Blocked by #(\d+)/g);
  const numbers: number[] = [];
  for (const match of matches) {
    const n = Number.parseInt(match[1], 10);
    if (Number.isFinite(n)) numbers.push(n);
  }
  return numbers;
}

/**
 * Extract a priority label (`p0`/`p1`/`p2`/`p3`) from a label list.
 * Returns `null` when no `priority/pN` label is present.
 */
export function extractPriority(labels: readonly string[]): string | null {
  for (const label of labels) {
    const match = label.match(/^priority\/(p[0-3])$/);
    if (match) return match[1];
  }
  return null;
}

/** Test-only hook — clear the caches between tests. */
export function __resetCaches(): void {
  issueCache.clear();
  searchCache.clear();
}
