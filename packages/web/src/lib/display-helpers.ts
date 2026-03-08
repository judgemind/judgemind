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
