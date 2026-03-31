/**
 * Static courthouse reference data — maps (county, department) to courthouse names.
 *
 * Courts encode courthouse identity in department names/codes differently per county:
 * - LA: department numbers map to specific courthouses
 * - OC: letter-prefixed codes (e.g. "C24" = Central Justice Center)
 * - Riverside: facility indicators in some department names
 * - SB: "R"/"S" prefix indicates Rancho Cucamonga vs San Bernardino
 * - San Diego: "C"/"N"/"E"/"S" prefix indicates facility
 *
 * This is reference data only — no DB table needed. Updated manually when
 * courthouse information changes (which is rare).
 */

export interface CourthouseInfo {
  /** Human-readable courthouse name. */
  name: string;
  /** Physical address (optional, for future use). */
  address?: string;
}

/**
 * Department-to-courthouse mapping rules per county.
 *
 * Each rule has a `match` function that tests a department string and
 * returns true if the department belongs to that courthouse.
 * Rules are evaluated in order — first match wins.
 */
interface CourthouseRule {
  courthouse: CourthouseInfo;
  match: (department: string) => boolean;
}

// ---------------------------------------------------------------------------
// Los Angeles
// ---------------------------------------------------------------------------

const LA_RULES: CourthouseRule[] = [
  // Stanley Mosk Courthouse — departments 1-99 (central civil)
  {
    courthouse: { name: 'Stanley Mosk Courthouse' },
    match: (dept) => {
      const n = parseInt(dept, 10);
      return !isNaN(n) && n >= 1 && n <= 99;
    },
  },
  // Spring Street Courthouse
  {
    courthouse: { name: 'Spring Street Courthouse' },
    match: (dept) => dept.startsWith('SS'),
  },
  // Chatsworth Courthouse
  {
    courthouse: { name: 'Chatsworth Courthouse' },
    match: (dept) => dept.startsWith('F'),
  },
  // Pasadena Courthouse
  {
    courthouse: { name: 'Pasadena Courthouse' },
    match: (dept) => dept.startsWith('P'),
  },
  // Pomona Courthouse South
  {
    courthouse: { name: 'Pomona Courthouse South' },
    match: (dept) => dept.startsWith('K'),
  },
  // Norwalk Courthouse
  {
    courthouse: { name: 'Norwalk Courthouse' },
    match: (dept) => dept.startsWith('C') || dept.startsWith('SE'),
  },
  // Van Nuys Courthouse
  {
    courthouse: { name: 'Van Nuys Courthouse East' },
    match: (dept) => dept.startsWith('W') || dept.startsWith('T'),
  },
  // Torrance Courthouse
  {
    courthouse: { name: 'Torrance Courthouse' },
    match: (dept) => dept.startsWith('B') || dept.startsWith('M'),
  },
  // Burbank Courthouse
  {
    courthouse: { name: 'Burbank Courthouse' },
    match: (dept) => dept.startsWith('NCC') || dept.startsWith('NCB'),
  },
  // Long Beach Courthouse
  {
    courthouse: { name: 'Long Beach Courthouse' },
    match: (dept) => dept.startsWith('S') && !dept.startsWith('SE') && !dept.startsWith('SS'),
  },
  // Compton Courthouse
  {
    courthouse: { name: 'Compton Courthouse' },
    match: (dept) => dept.startsWith('A'),
  },
];

// ---------------------------------------------------------------------------
// Orange County
// ---------------------------------------------------------------------------

const OC_RULES: CourthouseRule[] = [
  // Central Justice Center — departments starting with C or numeric
  {
    courthouse: { name: 'Central Justice Center, Santa Ana' },
    match: (dept) => {
      if (dept.startsWith('C')) return true;
      const n = parseInt(dept, 10);
      return !isNaN(n);
    },
  },
  // Lamoreaux Justice Center — departments starting with L
  {
    courthouse: { name: 'Lamoreaux Justice Center' },
    match: (dept) => dept.startsWith('L'),
  },
  // Harbor Justice Center — departments starting with H
  {
    courthouse: { name: 'Harbor Justice Center, Newport Beach' },
    match: (dept) => dept.startsWith('H'),
  },
  // North Justice Center — departments starting with N
  {
    courthouse: { name: 'North Justice Center, Fullerton' },
    match: (dept) => dept.startsWith('N'),
  },
  // West Justice Center — departments starting with W
  {
    courthouse: { name: 'West Justice Center, Westminster' },
    match: (dept) => dept.startsWith('W'),
  },
];

// ---------------------------------------------------------------------------
// Riverside
// ---------------------------------------------------------------------------

const RIVERSIDE_RULES: CourthouseRule[] = [
  // Riverside Historic Courthouse
  {
    courthouse: { name: 'Riverside Historic Courthouse' },
    match: (dept) => {
      const n = parseInt(dept, 10);
      return !isNaN(n) && n >= 1 && n <= 99;
    },
  },
  // Southwest Justice Center — departments starting with S
  {
    courthouse: { name: 'Southwest Justice Center, Murrieta' },
    match: (dept) => dept.startsWith('S'),
  },
  // Indio — departments starting with 2 or PSC
  {
    courthouse: { name: 'Larson Justice Center, Indio' },
    match: (dept) => dept.startsWith('2') || dept.startsWith('PSC'),
  },
];

// ---------------------------------------------------------------------------
// Santa Barbara
// ---------------------------------------------------------------------------

const SB_RULES: CourthouseRule[] = [
  // Santa Barbara — departments starting with numeric only (no prefix)
  {
    courthouse: { name: 'Santa Barbara Courthouse' },
    match: (dept) => {
      const n = parseInt(dept, 10);
      return !isNaN(n) && !dept.startsWith('SM');
    },
  },
  // Santa Maria — SM prefix
  {
    courthouse: { name: 'Santa Maria Courthouse' },
    match: (dept) => dept.startsWith('SM'),
  },
];

// ---------------------------------------------------------------------------
// San Bernardino
// ---------------------------------------------------------------------------

const SAN_BERNARDINO_RULES: CourthouseRule[] = [
  // San Bernardino — departments with R prefix (Rancho Cucamonga)
  {
    courthouse: { name: 'Rancho Cucamonga Courthouse' },
    match: (dept) => dept.startsWith('R'),
  },
  // San Bernardino — departments with S prefix
  {
    courthouse: { name: 'San Bernardino Justice Center' },
    match: (dept) => dept.startsWith('S'),
  },
  // Victorville — departments with V prefix
  {
    courthouse: { name: 'Victorville Courthouse' },
    match: (dept) => dept.startsWith('V'),
  },
  // Fallback — numeric departments
  {
    courthouse: { name: 'San Bernardino Justice Center' },
    match: (dept) => {
      const n = parseInt(dept, 10);
      return !isNaN(n);
    },
  },
];

// ---------------------------------------------------------------------------
// San Diego
// ---------------------------------------------------------------------------

const SAN_DIEGO_RULES: CourthouseRule[] = [
  // Central — C prefix
  {
    courthouse: { name: 'Central Courthouse, San Diego' },
    match: (dept) => dept.startsWith('C'),
  },
  // North — N prefix
  {
    courthouse: { name: 'North County Regional Center, Vista' },
    match: (dept) => dept.startsWith('N'),
  },
  // East — E prefix
  {
    courthouse: { name: 'East County Regional Center, El Cajon' },
    match: (dept) => dept.startsWith('E'),
  },
  // South — S prefix
  {
    courthouse: { name: 'South County Regional Center, Chula Vista' },
    match: (dept) => dept.startsWith('S'),
  },
];

// ---------------------------------------------------------------------------
// County → rules lookup
// ---------------------------------------------------------------------------

const COURTHOUSE_RULES: Record<string, CourthouseRule[]> = {
  'Los Angeles': LA_RULES,
  'Orange': OC_RULES,
  'Riverside': RIVERSIDE_RULES,
  'Santa Barbara': SB_RULES,
  'San Bernardino': SAN_BERNARDINO_RULES,
  'San Diego': SAN_DIEGO_RULES,
};

/**
 * Look up the courthouse for a given county + department.
 * Returns null if no mapping exists for this county or no rule matches.
 */
export function lookupCourthouse(county: string, department: string): string | null {
  const rules = COURTHOUSE_RULES[county];
  if (!rules) return null;

  const trimmed = department.trim();
  if (!trimmed) return null;

  for (const rule of rules) {
    if (rule.match(trimmed)) {
      return rule.courthouse.name;
    }
  }

  return null;
}

/**
 * Get all counties that have courthouse mapping data.
 */
export function getCountiesWithCourthouseData(): string[] {
  return Object.keys(COURTHOUSE_RULES);
}
