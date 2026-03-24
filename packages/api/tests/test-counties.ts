/**
 * Test Data Isolation — County Name Registry
 *
 * Integration tests share a single PostgreSQL database. To prevent
 * cross-test interference, each test file MUST use dedicated county names
 * (and court codes) that no other test file uses.
 *
 * HOW TO ADD A NEW TEST FILE:
 * 1. Add an entry to TEST_COUNTY_REGISTRY below.
 * 2. Import your counties from this module instead of hardcoding strings.
 * 3. The unit test (test-counties.unit.test.ts) will catch duplicate
 *    county names at CI time.
 *
 * WHY NOT RANDOM NAMES?
 * Random/unique-per-run names would avoid collisions but make assertions
 * harder — tests often need to assert exact county values in query results.
 * A static registry gives the best of both worlds: no collisions + readable
 * assertions.
 */

// ---------------------------------------------------------------------------
// Registry — one entry per integration test file
// ---------------------------------------------------------------------------

/**
 * Each entry maps a test file to the county names it is allowed to use.
 * The `courtCode` is a unique identifier used in the courts table to
 * prevent collisions even when two test files happen to share a county
 * name for different court records (they don't — but the court code
 * provides a second layer of safety).
 */
export const TEST_COUNTY_REGISTRY = {
  /**
   * graphql.integration.test.ts — core CRUD queries (case, judge, ruling)
   */
  graphql: {
    counties: ['Los Angeles'] as const,
    courtCodes: ['ca-la-test'] as const,
  },

  /**
   * data-quality.integration.test.ts — data quality metrics & overview
   *
   * Uses multiple counties to test health-status computation (green/yellow/red).
   * "Riverside" is reserved for aggregation tests to avoid polluting
   * the LA/OC/SD row counts that other describe blocks depend on.
   */
  dataQuality: {
    counties: ['Los Angeles', 'Orange', 'San Diego', 'Riverside'] as const,
    courtCodes: [] as const, // data_quality_metrics table has no court_code column
  },

  /**
   * search-rulings.integration.test.ts — full-text search via OpenSearch
   */
  searchRulings: {
    counties: ['Los Angeles'] as const,
    courtCodes: ['ca-la-search-test'] as const,
  },

  /**
   * judge-analytics.integration.test.ts — judge ruling aggregations
   */
  judgeAnalytics: {
    counties: ['San Francisco'] as const,
    courtCodes: ['ca-sf-analytics-test'] as const,
  },

  /**
   * alerts.integration.test.ts — alert subscriptions, evaluation, digests
   */
  alerts: {
    counties: ['Test Alert County'] as const,
    courtCodes: ['ca-alert-test'] as const,
  },

  /**
   * auth.integration.test.ts — user auth (register, login, tokens)
   *
   * Does not seed court/county data — only user rows.
   */
  auth: {
    counties: [] as const,
    courtCodes: [] as const,
  },
} as const;

// ---------------------------------------------------------------------------
// Collision detection — exported for use in the unit test
// ---------------------------------------------------------------------------

export interface CountyCollision {
  county: string;
  files: string[];
}

/**
 * Returns a list of county names that appear in more than one registry entry
 * where the overlap could cause cross-test interference.
 *
 * Sharing a county name is safe when both entries have distinct court_codes,
 * because their data is isolated by the court_code column (which has a
 * unique constraint). Tests query by courtId, not by county name.
 *
 * A collision is flagged when:
 * - Two or more entries without court codes share a county name
 *   (e.g. both write to `data_quality_metrics` which has no court_code column).
 * - One entry has no court code and another also has no court code, and they
 *   share a county name.
 *
 * Entries WITH court codes that share a county name are safe (isolated by
 * the court_code). This allows `graphql` and `searchRulings` to both use
 * "Los Angeles" with different court codes without triggering a false positive.
 */
export function findCountyCollisions(): CountyCollision[] {
  const countyToFiles = new Map<string, string[]>();

  for (const [file, entry] of Object.entries(TEST_COUNTY_REGISTRY)) {
    for (const county of entry.counties) {
      const files = countyToFiles.get(county) ?? [];
      files.push(file);
      countyToFiles.set(county, files);
    }
  }

  const collisions: CountyCollision[] = [];
  for (const [county, files] of countyToFiles) {
    if (files.length <= 1) continue;

    // Files WITHOUT court codes write to tables keyed by county name only
    // (e.g. data_quality_metrics). Two such files sharing a county = collision.
    const filesWithoutCourtCodes = files.filter(
      (f) => TEST_COUNTY_REGISTRY[f as keyof typeof TEST_COUNTY_REGISTRY].courtCodes.length === 0,
    );
    if (filesWithoutCourtCodes.length > 1) {
      collisions.push({ county, files: filesWithoutCourtCodes });
    }
  }

  return collisions;
}

/**
 * Checks for duplicate court codes across all registry entries.
 */
export function findCourtCodeCollisions(): { courtCode: string; files: string[] }[] {
  const codeToFiles = new Map<string, string[]>();

  for (const [file, entry] of Object.entries(TEST_COUNTY_REGISTRY)) {
    for (const code of entry.courtCodes) {
      const files = codeToFiles.get(code) ?? [];
      files.push(file);
      codeToFiles.set(code, files);
    }
  }

  return Array.from(codeToFiles.entries())
    .filter(([, files]) => files.length > 1)
    .map(([courtCode, files]) => ({ courtCode, files }));
}
