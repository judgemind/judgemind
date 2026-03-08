/** Build a human-readable heading from case data.
 *  When a case title is available it is returned as the heading
 *  (the case number should be shown separately as a subtitle).
 *  Without a title the case number is the heading. */
export function buildCaseHeading(
  caseData: { caseNumber: string; caseTitle: string | null } | null,
  fallbackId: string,
): string {
  if (!caseData) return `Case ${fallbackId}`;
  if (caseData.caseTitle) {
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

/** Remove lines that are page number artifacts. */
export function stripPageNumbers(text: string): string {
  return text
    .split('\n')
    .filter((line) => !PAGE_NUMBER_PATTERNS.some((p) => p.test(line)))
    .join('\n');
}

/**
 * Clean ruling text for display.
 *
 * Applies encoding fixes, strips page numbers, and collapses excessive
 * blank lines. Returns an array of paragraph strings suitable for
 * rendering as separate `<p>` elements.
 */
export function cleanRulingText(text: string): string[] {
  let cleaned = fixEncoding(text);
  cleaned = stripPageNumbers(cleaned);

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
