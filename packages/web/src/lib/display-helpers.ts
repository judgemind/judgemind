/** Build a human-readable heading from case data.
 *  Always returns the case number as the heading.
 *  The case title (when available) should be rendered separately
 *  as a subtitle by the caller. */
export function buildCaseHeading(
  caseData: { caseNumber: string } | null,
  fallbackId: string,
): string {
  if (!caseData) return `Case ${fallbackId}`;
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

/**
 * Clean ruling text for display.
 *
 * Applies encoding fixes, strips page numbers, detects paragraph
 * boundaries, and collapses excessive blank lines. Returns an array
 * of paragraph strings suitable for rendering as separate `<p>` elements.
 */
export function cleanRulingText(text: string): string[] {
  let cleaned = fixEncoding(text);
  cleaned = stripPageNumbers(cleaned);
  cleaned = stripBoilerplate(cleaned);

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
