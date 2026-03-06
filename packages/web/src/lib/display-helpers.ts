/** Build a human-readable heading from case data. */
export function buildCaseHeading(
  caseData: { caseNumber: string; caseTitle: string | null } | null,
  fallbackId: string,
): string {
  if (!caseData) return `Case ${fallbackId}`;
  if (caseData.caseTitle) {
    return `${caseData.caseTitle} \u2014 ${caseData.caseNumber}`;
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
