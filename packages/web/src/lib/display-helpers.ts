/** Build a human-readable heading from case data.
 *  Returns the case number as the heading, except for UNKNOWN placeholders
 *  where it falls back to `caseTitle` when available. */
export function buildCaseHeading(
  caseData: { caseNumber: string; caseTitle?: string | null } | null,
  fallbackId: string,
): string {
  if (!caseData) return `Case ${fallbackId}`;
  if (caseData.caseNumber.startsWith('UNKNOWN') && caseData.caseTitle?.trim()) {
    return caseData.caseTitle;
  }
  return caseData.caseNumber;
}

/** Build a human-readable heading from judge data. */
export function buildJudgeHeading(
  judgeData: { canonicalName: string } | null,
  fallbackId: string,
): string {
  if (!judgeData) return `Judge ${fallbackId}`;
  return `Judge ${judgeData.canonicalName}`;
}

// ---------------------------------------------------------------------------
// Generic label formatting (shared between server & client components)
// ---------------------------------------------------------------------------

/**
 * Known full label mappings (lowercased key -> display label).
 * Checked before generic title-case logic so compound terms render correctly.
 */
const LABEL_MAP: Record<string, string> = {
  anti_slapp: 'Anti-SLAPP',
};

/** Abbreviations that should stay fully uppercase. */
const UPPERCASE_LABEL_WORDS = new Set(['msj', 'mtd', 'mil']);

/** Format a snake_case string to Title Case, preserving known abbreviations.
 *  Returns an em-dash for null values. */
export function formatLabel(value: string | null): string {
  if (!value) return '\u2014';
  const key = value.toLowerCase();
  if (LABEL_MAP[key]) return LABEL_MAP[key];
  return key
    .replace(/_/g, ' ')
    .split(' ')
    .map((word) => UPPERCASE_LABEL_WORDS.has(word) ? word.toUpperCase() : (word.charAt(0).toUpperCase() + word.slice(1)))
    .join(' ');
}

// ---------------------------------------------------------------------------
// Date & outcome formatting (shared between server & client components)
// ---------------------------------------------------------------------------

/** Format an ISO 8601 date string as a short readable date (e.g. "Mar 5, 2026").
 *  Returns "Date unknown" for null, undefined, or unparseable inputs. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return 'Date unknown';
  const d = new Date(iso + 'T00:00:00Z');
  if (isNaN(d.getTime())) return 'Date unknown';
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/** Convert a snake_case outcome code to a human-readable label. Returns "Not classified" for null. */
export function formatOutcome(outcome: string | null): string {
  if (!outcome) return 'Not classified';
  return outcome
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ---------------------------------------------------------------------------
// Outcome badge styling (shared across all components)
// ---------------------------------------------------------------------------

/** Badge variant type matching shadcn Badge component. */
export type OutcomeBadgeVariant = 'default' | 'secondary' | 'destructive' | 'outline';

/** Mapping from outcome code to shadcn Badge variant. */
const OUTCOME_VARIANT_MAP: Record<string, OutcomeBadgeVariant> = {
  granted: 'default',
  denied: 'destructive',
  granted_in_part: 'secondary',
  denied_in_part: 'secondary',
  moot: 'outline',
  continued: 'outline',
  other: 'outline',
};

/**
 * Get the shadcn Badge variant for a ruling outcome.
 * Returns 'outline' for null or unknown outcomes.
 */
export function getOutcomeBadgeVariant(outcome: string | null): OutcomeBadgeVariant {
  if (!outcome) return 'outline';
  return OUTCOME_VARIANT_MAP[outcome] ?? 'outline';
}

/** Mapping from outcome code to Tailwind CSS class strings (with dark mode). */
const OUTCOME_CLASS_MAP: Record<string, string> = {
  granted:
    'bg-green-100 text-green-800 border-green-200 dark:bg-green-900 dark:text-green-200 dark:border-green-800',
  denied:
    'bg-red-100 text-red-800 border-red-200 dark:bg-red-900 dark:text-red-200 dark:border-red-800',
  granted_in_part:
    'bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-900 dark:text-yellow-200 dark:border-yellow-800',
  denied_in_part:
    'bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-900 dark:text-yellow-200 dark:border-yellow-800',
};

/** Default CSS class for outcomes with no specific styling. */
const OUTCOME_CLASS_FALLBACK =
  'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-700 dark:text-slate-300 dark:border-slate-600';

/**
 * Get the Tailwind CSS class string for a ruling outcome badge.
 * Used by components not yet migrated to shadcn Badge variants.
 * Returns a neutral slate style for null or unknown outcomes.
 */
export function getOutcomeBadgeClass(outcome: string | null): string {
  if (!outcome) return OUTCOME_CLASS_FALLBACK;
  return OUTCOME_CLASS_MAP[outcome] ?? OUTCOME_CLASS_FALLBACK;
}

// ---------------------------------------------------------------------------
// Summary cleanup (display-time)
// ---------------------------------------------------------------------------

/** Pattern for leading markdown headings like "# Summary", "## Summary", etc. */
const SUMMARY_HEADING_RE = /^#{1,6}\s+.*\n*/;

/**
 * Patterns that repeat metadata already shown in the ruling page header.
 * Each regex matches a full line (anchored with ^ and terminated by newline or end).
 */
const SUMMARY_METADATA_LINE_PATTERNS: RegExp[] = [
  /^\s*\*{0,2}(?:case\s+(?:number|no\.?)|case\s*#)\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}judge\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}(?:parties|party)\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}court\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}department\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}hearing\s+date\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}county\s*[:;]\*{0,2}\s*.+$/im,
  /^\s*\*{0,2}motion\s+type\s*[:;]\*{0,2}\s*.+$/im,
];

/**
 * Clean an AI-generated summary for display on the ruling detail page.
 *
 * - Strips leading markdown headings (`# Summary`, `## Summary`, etc.)
 * - Strips lines that repeat metadata already shown in the page header
 *   (case number, judge, parties, court, department, hearing date, county, motion type)
 * - Collapses excessive whitespace
 */
export function cleanSummary(summary: string): string {
  if (!summary) return summary;

  let cleaned = summary;

  // Strip leading markdown heading (e.g. "# Summary\n")
  cleaned = cleaned.replace(SUMMARY_HEADING_RE, '');

  // Strip metadata lines that duplicate the page header
  for (const pattern of SUMMARY_METADATA_LINE_PATTERNS) {
    cleaned = cleaned.replace(pattern, '');
  }

  // Collapse multiple blank lines into one and trim
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim();

  return cleaned;
}

// ---------------------------------------------------------------------------
// Ruling text cleanup (display-time)
// ---------------------------------------------------------------------------
// Cleans ruling text for display in the frontend. This is a safety net for
// older rulings that were stored before ingestion-time cleanup was added,
// and also handles any artifacts the backend cleanup may have missed.

/**
 * Common mojibake replacements for text that was double-encoded or had
 * charset mismatches (Windows-1252 interpreted as UTF-8).
 */
const MOJIBAKE_MAP: [RegExp, string][] = [
  [/\u00e2\u0080\u009c/g, '\u201c'], // left double quote
  [/\u00e2\u0080\u009d/g, '\u201d'], // right double quote
  [/\u00e2\u0080\u0098/g, '\u2018'], // left single quote
  [/\u00e2\u0080\u0099/g, '\u2019'], // right single quote
  [/\u00e2\u0080\u0093/g, '\u2013'], // en dash
  [/\u00e2\u0080\u0094/g, '\u2014'], // em dash
  [/\u00e2\u0080\u00a6/g, '\u2026'], // horizontal ellipsis
  [/\u00c2\u00a7/g, '\u00a7'],       // section sign (double-encoded)
  [/\u00c2\u00b6/g, '\u00b6'],       // pilcrow (double-encoded)
  [/\u00bf/g, "'"],                   // inverted question mark -> apostrophe
  [/\u00c2\u00a0/g, ' '],            // double-encoded NBSP
  [/\u00a0/g, ' '],                   // non-breaking space
];

/** Fix common encoding errors (mojibake) in ruling text. */
export function fixEncoding(text: string): string {
  let result = text;
  for (const [pattern, replacement] of MOJIBAKE_MAP) {
    result = result.replace(pattern, replacement);
  }
  return result;
}

/** Page number line patterns to strip. */
const PAGE_NUMBER_PATTERNS: RegExp[] = [
  /^\s*page\s+\d+\s+of\s+\d+\s*$/i,
  /^\s*-{1,2}\s*\d+\s*-{1,2}\s*$/,
  /^\s*\d{1,3}\s*$/,
];

// ---------------------------------------------------------------------------
// Boilerplate patterns (display-time safety net)
// ---------------------------------------------------------------------------

/** Single-line boilerplate patterns to strip. */
const BOILERPLATE_PATTERNS: RegExp[] = [
  /^\s*SUPERIOR\s+COURT\s+OF\s+(?:THE\s+STATE\s+OF\s+)?CALIFORNIA\s*$/i,
  /^\s*COUNTY\s+OF\s+\w[\w\s]*$/i,
  /^\s*(?:DEPARTMENT|DEPT\.?)\s+\S+\s*$/i,
  /^\s*(?:parties\s+who\s+intend\s+to\s+submit|if\s+you\s+intend\s+to\s+submit|unless\s+.*\s+notif(?:y|ies)|parties\s+should\s+notify|the\s+court\s+will\s+prepare|if\s+the\s+parties\s+neither)/i,
];

/** Multi-line block start patterns — when matched, consecutive non-blank
 *  lines from this point are removed (up to MAX_BLOCK_LINES). */
const BLOCK_START_PATTERNS: RegExp[] = [
  /^\s*(?:DEPARTMENT|DEPT\.?)\s+\S+\s+LAW\s+AND\s+MOTION\s+RULINGS?\s*$/i,
  /^\s*if\s+you\s+wish\s+to\s+submit\s+on\s+the\s+tentative/i,
  /^\s*if\s+you\s+intend\s+to\s+submit\s+on\s+this\s+tentative/i,
];

/** Maximum number of lines a boilerplate block can consume. Prevents runaway
 *  removal when the source text has no blank lines (e.g. LA ruling PDFs).
 *  Boilerplate blocks are typically 1 header + 2-4 instruction lines. */
const MAX_BLOCK_LINES = 5;

/** Remove boilerplate header/instruction lines and multi-line blocks. */
export function stripBoilerplate(text: string): string {
  const lines = text.split('\n');
  const cleaned: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // Single-line boilerplate
    if (BOILERPLATE_PATTERNS.some((p) => p.test(line))) {
      i++;
      continue;
    }

    // Multi-line block start — remove up to MAX_BLOCK_LINES consecutive
    // non-blank lines. The cap prevents runaway removal when the source
    // text has no blank lines separating boilerplate from content.
    if (BLOCK_START_PATTERNS.some((p) => p.test(line))) {
      let removed = 0;
      while (
        i < lines.length &&
        lines[i].trim().length > 0 &&
        removed < MAX_BLOCK_LINES
      ) {
        i++;
        removed++;
      }
      continue;
    }

    cleaned.push(line);
    i++;
  }
  return cleaned.join('\n');
}

/** Remove lines that are page number artifacts. */
export function stripPageNumbers(text: string): string {
  return text
    .split('\n')
    .filter((line) => !PAGE_NUMBER_PATTERNS.some((p) => p.test(line)))
    .join('\n');
}

// ---------------------------------------------------------------------------
// Paragraph detection (display-time fallback)
// ---------------------------------------------------------------------------
// PDF-extracted text often has single newlines at line breaks but no
// double-newline separators between paragraphs. These heuristics detect
// paragraph boundaries and insert double-newline separators.

/** Known ALL CAPS section headers in California court rulings. */
const KNOWN_HEADERS = new Set([
  'BACKGROUND',
  'DISCUSSION',
  'RULING',
  'ORDER',
  'ANALYSIS',
  'CONCLUSION',
  'LEGAL STANDARD',
  'FACTUAL BACKGROUND',
  'PROCEDURAL HISTORY',
  'TENTATIVE RULING',
  'MOTION',
  'OPPOSITION',
  'REPLY',
  'FINDINGS',
  'STATEMENT OF DECISION',
]);

/** Pattern for ALL CAPS lines (potential section headers). */
const SECTION_HEADER_RE = /^\s*[A-Z][A-Z /&]+[A-Z]\s*$/;

/** Pattern for indented lines (4+ spaces or tab). */
const INDENT_RE = /^(?:    |\t)/;

/** Visual separator lines: underscores, dashes, equals, or asterisks (3+). */
const SEPARATOR_RE = /^\s*[_\-=*]{3,}\s*$/;

/** Check whether a line looks like an ALL CAPS section header. */
function isSectionHeader(line: string): boolean {
  const stripped = line.trim();
  if (!stripped) return false;
  if (KNOWN_HEADERS.has(stripped)) return true;
  if (stripped.length >= 3 && SECTION_HEADER_RE.test(line)) return true;
  return false;
}

/**
 * Detect paragraph boundaries in single-newline text.
 *
 * Inserts double-newline separators at detected paragraph boundaries.
 * If the text already contains double newlines, it is returned as-is.
 */
export function detectParagraphs(text: string): string {
  if (!text || text.includes('\n\n')) return text;

  const lines = text.split('\n');
  if (lines.length <= 1) return text;

  // Compute average non-empty line length for short-line heuristic.
  // Exclude separator lines so they don't skew the average.
  const nonEmptyLengths = lines
    .filter((l) => l.trim().length > 0 && !SEPARATOR_RE.test(l))
    .map((l) => l.length);
  const avgLen =
    nonEmptyLengths.length > 0
      ? nonEmptyLengths.reduce((a, b) => a + b, 0) / nonEmptyLengths.length
      : 0;

  const result: string[] = [];

  // Handle first line: separator becomes empty line
  if (SEPARATOR_RE.test(lines[0])) {
    result.push('');
  } else {
    result.push(lines[0]);
  }

  for (let i = 1; i < lines.length; i++) {
    const current = lines[i];
    const prev = lines[i - 1];

    // Heuristic 0: Separator lines become paragraph breaks
    if (SEPARATOR_RE.test(current)) {
      result.push('');
      continue;
    }

    let insertBreak = false;

    // Heuristic 1: Section headers in ALL CAPS
    if (isSectionHeader(current)) {
      insertBreak = true;
    }
    // Heuristic 2: Indentation change (non-indented -> indented)
    else if (
      INDENT_RE.test(current) &&
      prev.trim().length > 0 &&
      !INDENT_RE.test(prev)
    ) {
      insertBreak = true;
    }
    // Heuristic 3: Short previous line + current starts with capital
    else if (
      prev.trim().length > 0 &&
      current.trim().length > 0 &&
      prev.trimEnd().length < avgLen * 0.6 &&
      avgLen > 20 &&
      /^[A-Z]/.test(current.trimStart()) &&
      /[.:;!?]$/.test(prev.trimEnd())
    ) {
      insertBreak = true;
    }

    if (insertBreak) {
      result.push(''); // blank line = paragraph separator
    }
    result.push(current);
  }

  return result.join('\n');
}

// ---------------------------------------------------------------------------
// Motion type formatting
// ---------------------------------------------------------------------------

/**
 * Known full motion type mappings (lowercased key -> display label).
 * Checked before the generic title-case logic so compound terms like
 * "anti_slapp" render correctly.
 */
const MOTION_TYPE_MAP: Record<string, string> = {
  anti_slapp: 'Anti-SLAPP',
};

/** Abbreviations that should stay fully uppercase. */
const UPPERCASE_MOTION_WORDS = new Set(['msj', 'mtd', 'mil']);

/** Small words that stay lowercase unless they are the first word. */
const LOWERCASE_WORDS = new Set(['to', 'for', 'of', 'in', 'on', 'the', 'a', 'an']);

/** Format a motion type for display, returning a placeholder for null values. */
export function formatMotionType(motionType: string | null): string {
  if (!motionType) return 'Not classified';
  const key = motionType.toLowerCase();
  if (MOTION_TYPE_MAP[key]) return MOTION_TYPE_MAP[key];
  return key
    .replace(/_/g, ' ')
    .split(' ')
    .map((word, i) => {
      if (UPPERCASE_MOTION_WORDS.has(word)) return word.toUpperCase();
      if (i > 0 && LOWERCASE_WORDS.has(word)) return word;
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ');
}

// ---------------------------------------------------------------------------
// Judge name formatting
// ---------------------------------------------------------------------------

/** Format a judge name for display, returning a placeholder for null values. */
export function formatJudgeName(
  judge: { canonicalName: string } | null,
): string {
  if (!judge) return 'Unknown judge';
  return judge.canonicalName;
}

// ---------------------------------------------------------------------------
// Document helpers
// ---------------------------------------------------------------------------

/** Map document format to a human-readable label. */
export const FORMAT_LABELS: Record<string, string> = {
  pdf: 'PDF',
  html: 'HTML',
  txt: 'TXT',
  docx: 'DOCX',
};

/** Build the download URL for a document. Uses the same origin as the GraphQL API. */
export function buildDownloadUrl(documentId: string): string {
  const graphqlUrl = process.env.NEXT_PUBLIC_GRAPHQL_URL ?? 'http://localhost:3001/graphql';
  const baseUrl = graphqlUrl.replace(/\/graphql$/, '');
  return `${baseUrl}/api/documents/${documentId}/download`;
}

// ---------------------------------------------------------------------------
// Text truncation
// ---------------------------------------------------------------------------

/** Number of characters shown before truncation. */
export const RULING_TEXT_TRUNCATE_LENGTH = 500;

/**
 * Truncate text to `maxLen` characters, breaking at the last whitespace
 * boundary before the limit. Returns the original string if it fits.
 */
export function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  const truncated = text.slice(0, maxLen);
  const lastSpace = truncated.lastIndexOf(' ');
  // If there's a space in the first half, break there; otherwise hard-cut.
  if (lastSpace > maxLen / 2) {
    return truncated.slice(0, lastSpace) + '\u2026';
  }
  return truncated + '\u2026';
}

// ---------------------------------------------------------------------------
// Party grouping
// ---------------------------------------------------------------------------

/** Party roles grouped as plaintiffs. */
const PLAINTIFF_ROLES = new Set(['plaintiff', 'petitioner', 'cross_complainant', 'moving_party']);

/** Party roles grouped as defendants. */
const DEFENDANT_ROLES = new Set(['defendant', 'respondent', 'cross_defendant', 'responding_party']);

/**
 * Group parties into plaintiffs, defendants, and other columns.
 * Uses the case-specific ``role`` from the case_parties join table
 * (plaintiff, defendant, petitioner, etc.). Falls back to ``partyType``
 * for legacy data that only has entity classification.
 */
export function groupParties(
  parties: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>,
): {
  plaintiffs: typeof parties;
  defendants: typeof parties;
  others: typeof parties;
} {
  // Deduplicate parties by (id, role) to handle any duplicate rows from the DB.
  const seen = new Set<string>();
  const deduped: typeof parties = [];
  for (const party of parties) {
    const dedupKey = `${party.id}:${party.role ?? ''}`;
    if (!seen.has(dedupKey)) {
      seen.add(dedupKey);
      deduped.push(party);
    }
  }

  const plaintiffs: typeof parties = [];
  const defendants: typeof parties = [];
  const others: typeof parties = [];
  for (const party of deduped) {
    const key = (party.role ?? party.partyType ?? '').toLowerCase();
    if (PLAINTIFF_ROLES.has(key)) {
      plaintiffs.push(party);
    } else if (DEFENDANT_ROLES.has(key)) {
      defendants.push(party);
    } else {
      others.push(party);
    }
  }
  return { plaintiffs, defendants, others };
}

// ---------------------------------------------------------------------------
// Metadata header stripping (display-time)
// ---------------------------------------------------------------------------
// Strips redundant metadata boilerplate from the beginning of ruling text.
// Court ruling text often starts with a header block that repeats metadata
// already displayed in the UI (date, department, judge, case number, parties,
// motion type). This function removes those leading lines so the user sees
// substantive content first.

/** Metadata fields available for matching against ruling text headers. */
export interface RulingMetadata {
  caseNumber?: string;
  caseTitle?: string;
  judgeName?: string;
  department?: string;
  hearingDate?: string; // ISO 8601 (YYYY-MM-DD)
  motionType?: string;  // snake_case (e.g. "motion_to_strike")
}

/** Month names for date format generation. */
const MONTH_NAMES = [
  'january', 'february', 'march', 'april', 'may', 'june',
  'july', 'august', 'september', 'october', 'november', 'december',
];

/** Known header prefixes that introduce metadata blocks. */
const HEADER_PREFIX_RE = /^\s*(?:tentative\s+rulings?|tentative\s+case\s+management\s+order|probate\s+notes|minute\s+order|court\s+rulings?)\b/i;

/**
 * Build an array of date format strings from an ISO date for matching.
 * For "2026-03-20" produces: ["march 20, 2026", "03/20/2026", "3/20/2026", "2026-03-20", "march 20 2026", "mar 20, 2026", "mar 20 2026"].
 */
function buildDatePatterns(isoDate: string): string[] {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!match) return [];
  const year = match[1];
  const monthNum = parseInt(match[2], 10);
  const day = parseInt(match[3], 10);
  const monthName = MONTH_NAMES[monthNum - 1];
  const monthAbbrev = monthName.slice(0, 3);
  const paddedMonth = match[2];
  const paddedDay = match[3];

  return [
    `${monthName} ${day}, ${year}`,
    `${monthName} ${day} ${year}`,
    `${monthAbbrev} ${day}, ${year}`,
    `${monthAbbrev} ${day} ${year}`,
    `${paddedMonth}/${paddedDay}/${year}`,
    `${monthNum}/${day}/${year}`,
    isoDate,
  ];
}

/**
 * Format a snake_case motion type to a human-readable form for matching.
 * E.g., "motion_to_strike" -> "motion to strike".
 */
function motionTypeToWords(motionType: string): string {
  return motionType.toLowerCase().replace(/_/g, ' ');
}

/**
 * Check whether a line looks like a metadata line given the ruling's known metadata.
 *
 * A line is metadata if:
 * - It matches a known header prefix (e.g. "Tentative Ruling", "Probate Notes")
 * - It contains the case number
 * - It contains a date format matching the hearing date
 * - It contains "department" followed by the department value
 * - It contains the judge name (or "Judge" followed by parts of the name)
 * - It looks like a party/case-title line (contains significant words from case title)
 * - It starts with "Motion:" and contains the motion type words
 * - It is a "Case No." / "Case Number:" line containing the case number
 */
/** Test whether `word` appears as a whole word in `text` (case-insensitive). */
function containsWord(text: string, word: string): boolean {
  return new RegExp(`\\b${escapeRegex(word)}\\b`, 'i').test(text);
}

function isMetadataLine(line: string, metadata: RulingMetadata): boolean {
  const trimmed = line.trim();
  if (trimmed.length === 0) return false; // blank lines are handled separately
  const lower = trimmed.toLowerCase();

  // Known header prefixes
  if (HEADER_PREFIX_RE.test(trimmed)) return true;

  // Case number match — use word boundary to avoid partial matches (e.g. "25" in "Rule 25.1")
  if (metadata.caseNumber) {
    const caseNumLower = metadata.caseNumber.toLowerCase();
    if (containsWord(lower, caseNumLower)) return true;
  }

  // Date match — date strings are specific enough that substring is safe
  if (metadata.hearingDate) {
    const datePatterns = buildDatePatterns(metadata.hearingDate);
    if (datePatterns.some((dp) => lower.includes(dp))) return true;
    // Also check for "hearing date:" prefix
    if (/^\s*hearing\s+date\s*:/i.test(trimmed)) return true;
  }

  // Department match — require "department" or "dept" prefix
  if (metadata.department) {
    const deptLower = metadata.department.toLowerCase();
    const deptRe = new RegExp(`\\b(?:department|dept\\.?)\\s+${escapeRegex(deptLower)}\\b`, 'i');
    if (deptRe.test(trimmed)) return true;
  }

  // Judge name match — use word boundaries to avoid "Smith" matching "blacksmith"
  if (metadata.judgeName) {
    const judgeLower = metadata.judgeName.toLowerCase();
    if (containsWord(lower, judgeLower)) return true;
    // Match "Judge LastName" pattern (just the last name)
    const nameParts = judgeLower.split(/[\s,]+/).filter((p) => p.length > 2);
    if (nameParts.length > 0 && /^\s*judge\s+/i.test(trimmed)) {
      if (nameParts.some((part) => containsWord(lower, part))) return true;
    }
  }

  // Case title / party names match — if enough significant words from the
  // case title appear on this line, it's likely a party header line
  if (metadata.caseTitle) {
    // Filter out common non-substantive short words but preserve short names
    const NOISE_WORDS = new Set([
      'v', 'vs', 'et', 'al', 'in', 're', 'on', 'of', 'the', 'a', 'an', 'and', 'for', 'to',
    ]);
    const titleWords = metadata.caseTitle
      .toLowerCase()
      .split(/[\s,;.]+/)
      .filter((w) => w.length > 1 && !NOISE_WORDS.has(w));
    if (titleWords.length > 0) {
      const matchCount = titleWords.filter((w) => containsWord(lower, w)).length;
      // Require at least 2 matched words (or all words for single-word titles)
      // to prevent false positives from common names appearing in substantive text
      if (titleWords.length === 1 && matchCount === 1) return true;
      if (titleWords.length > 1 && matchCount >= 2 && matchCount >= Math.ceil(titleWords.length * 0.5)) return true;
    }
  }

  // Motion type match — "Motion:" prefix lines
  if (metadata.motionType) {
    const motionWords = motionTypeToWords(metadata.motionType);
    if (/^\s*motion\s*:/i.test(trimmed) && containsWord(lower, motionWords)) return true;
    // Also match lines that are just the motion type
    if (lower === motionWords) return true;
  }

  return false;
}

/** Escape special regex characters in a string. */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Strip redundant metadata header from the beginning of ruling text.
 *
 * Examines lines from the start of the text. Lines that contain
 * known metadata values (case number, date, department, judge name,
 * party names, motion type) or match header prefixes are removed.
 * Blank lines interspersed in the metadata header are also removed.
 * Stripping stops at the first non-metadata, non-blank line.
 *
 * If ALL lines would be stripped, the original text is returned
 * to avoid losing content entirely.
 *
 * @param text - The ruling text to clean
 * @param metadata - Known metadata fields for matching
 * @returns The text with the leading metadata header removed
 */
export function stripMetadataHeader(text: string, metadata: RulingMetadata): string {
  if (!text) return text;

  const hasAnyMetadata = Object.values(metadata).some((v) => v != null && v !== '');
  if (!hasAnyMetadata) return text;

  const lines = text.split('\n');
  let stripUntil = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().length === 0) {
      // Blank line — might be within the metadata header or at its end.
      // Look ahead: if the next non-blank line is also metadata, continue.
      // Otherwise, this blank line marks the end of the header.
      let nextNonBlank = i + 1;
      while (nextNonBlank < lines.length && lines[nextNonBlank].trim().length === 0) {
        nextNonBlank++;
      }
      if (nextNonBlank < lines.length && isMetadataLine(lines[nextNonBlank], metadata)) {
        // Blank line within metadata — skip it
        continue;
      }
      // Blank line at end of metadata header — include it in the strip range
      stripUntil = i + 1;
      break;
    }

    if (isMetadataLine(line, metadata)) {
      stripUntil = i + 1;
    } else {
      // First non-metadata line — stop
      break;
    }
  }

  // If we would strip everything, return original
  if (stripUntil >= lines.length) return text;

  // Skip any leading blank lines after the stripped header
  let start = stripUntil;
  while (start < lines.length && lines[start].trim().length === 0) {
    start++;
  }

  // If stripping would leave nothing, return original
  if (start >= lines.length) return text;

  return lines.slice(start).join('\n');
}

/**
 * Strip leading HTML block elements whose text content matches metadata.
 *
 * Processes sanitized ruling HTML by removing leading `<p>`, `<div>`,
 * `<h1>`-`<h6>` elements at the start of the HTML whose text content
 * matches metadata patterns. Stops at the first block element that
 * contains substantive (non-metadata) content.
 *
 * @param html - The sanitized ruling HTML
 * @param metadata - Known metadata fields for matching
 * @returns The HTML with leading metadata elements removed
 */
export function stripMetadataHeaderHtml(html: string, metadata: RulingMetadata): string {
  if (!html) return html;

  const hasAnyMetadata = Object.values(metadata).some((v) => v != null && v !== '');
  if (!hasAnyMetadata) return html;

  // Match leading block-level elements and check their text content.
  // This regex captures the tag name and uses a backreference to ensure
  // the closing tag matches the opening tag.
  const blockTagRe = /^(\s*<((?:p|div|h[1-6]))(?:\s[^>]*)?>)([\s\S]*?)(<\/\2>)/i;
  const leadingWhitespaceRe = /^\s*(?:<br\s*\/?>|\s)*/i;

  let remaining = html.trimStart();
  let strippedAny = false;

  // Iteratively remove leading block elements that are metadata
  for (let guard = 0; guard < 20; guard++) {
    // Skip leading whitespace and <br> tags
    remaining = remaining.replace(leadingWhitespaceRe, '');
    if (!remaining) break;

    const match = blockTagRe.exec(remaining);
    if (!match) break;

    // Extract text content (strip inner HTML tags, replace <br> with space first)
    const innerHtml = match[3];
    const textContent = innerHtml.replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]*>/g, '').trim();

    if (!textContent) {
      // Empty element — skip it
      remaining = remaining.slice(match[0].length);
      strippedAny = true;
      continue;
    }

    if (isMetadataLine(textContent, metadata)) {
      remaining = remaining.slice(match[0].length);
      strippedAny = true;
    } else {
      // Non-metadata element — stop stripping
      break;
    }
  }

  // If nothing was stripped, return original
  if (!strippedAny) return html;

  // If stripping would leave nothing, return original
  const trimmedRemaining = remaining.trim();
  if (!trimmedRemaining) return html;

  return trimmedRemaining;
}

// ---------------------------------------------------------------------------
// Ruling text cleanup (display-time)
// ---------------------------------------------------------------------------

/**
 * Clean ruling text for display.
 *
 * Applies encoding fixes, strips page numbers, metadata headers (when
 * metadata is provided), boilerplate, detects paragraph boundaries, and
 * collapses excessive blank lines. Returns an array of paragraph strings
 * suitable for rendering as separate `<p>` elements.
 *
 * @param text - The raw ruling text
 * @param metadata - Optional ruling metadata for header stripping
 */
export function cleanRulingText(text: string, metadata?: RulingMetadata): string[] {
  let cleaned = fixEncoding(text);
  cleaned = stripPageNumbers(cleaned);
  cleaned = stripBoilerplate(cleaned);

  // Strip metadata header if metadata is provided
  if (metadata) {
    cleaned = stripMetadataHeader(cleaned, metadata);
  }

  // Detect paragraph boundaries for text that only has single newlines
  cleaned = detectParagraphs(cleaned);

  // Strip trailing whitespace per line
  cleaned = cleaned
    .split('\n')
    .map((line) => line.trimEnd())
    .join('\n');

  // Split into paragraphs on double-newlines (or more).
  // A "paragraph break" is two or more consecutive newlines.
  const paragraphs = cleaned
    .split(/\n{2,}/)
    .map((p) => p.replace(/\n/g, ' ').trim())
    .filter((p) => p.length > 0);

  return paragraphs;
}
