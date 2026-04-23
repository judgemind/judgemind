export interface DigestAlertItem {
  alertType: string;
  caseNumber?: string;
  caseTitle?: string;
  judgeName?: string;
  hearingDate?: string;
  outcome?: string;
  motionType?: string;
  /** Short excerpt from the ruling text, if available. */
  excerpt?: string;
}

export interface DigestTemplateParams {
  displayName?: string;
  alerts: DigestAlertItem[];
  /** ISO date string for the digest, e.g. "2026-03-11". */
  digestDate: string;
}

function alertTypeLabel(alertType: string): string {
  switch (alertType) {
    case 'judge_ruling':
      return 'Judge Ruling';
    case 'case_docket':
      return 'Case Activity';
    case 'keyword':
      return 'Keyword Match';
    case 'party_attorney':
      return 'Party/Attorney';
    default:
      return alertType;
  }
}

function renderAlertItemText(item: DigestAlertItem): string {
  const lines: string[] = [];
  lines.push(`  [${alertTypeLabel(item.alertType)}]`);
  if (item.caseNumber) lines.push(`  Case: ${item.caseNumber}${item.caseTitle ? ` — ${item.caseTitle}` : ''}`);
  if (item.judgeName) lines.push(`  Judge: ${item.judgeName}`);
  if (item.hearingDate) lines.push(`  Hearing: ${item.hearingDate}`);
  if (item.outcome) lines.push(`  Outcome: ${item.outcome}`);
  if (item.motionType) lines.push(`  Motion: ${item.motionType}`);
  if (item.excerpt) lines.push(`  Excerpt: ${item.excerpt}`);
  return lines.join('\n');
}

function renderAlertItemHtml(item: DigestAlertItem): string {
  let html = `<tr><td style="padding: 12px; border-bottom: 1px solid #eee;">`;
  html += `<span style="display: inline-block; background: #e8f0fe; color: #1a56db; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-bottom: 4px;">${alertTypeLabel(item.alertType)}</span>`;
  if (item.caseNumber) {
    html += `<br/><strong>${item.caseNumber}</strong>`;
    if (item.caseTitle) html += ` &mdash; ${item.caseTitle}`;
  }
  if (item.judgeName) html += `<br/>Judge: ${item.judgeName}`;
  if (item.hearingDate) html += `<br/>Hearing: ${item.hearingDate}`;
  if (item.outcome || item.motionType) {
    html += '<br/>';
    if (item.motionType) html += `Motion: ${item.motionType}`;
    if (item.outcome) html += `${item.motionType ? ' | ' : ''}Outcome: ${item.outcome}`;
  }
  if (item.excerpt) {
    html += `<br/><span style="color: #555; font-size: 13px;">${item.excerpt}</span>`;
  }
  html += `</td></tr>`;
  return html;
}

export function renderDigestEmail({
  displayName,
  alerts,
  digestDate,
}: DigestTemplateParams): { subject: string; html: string; text: string } {
  const greeting = displayName ? `Hi ${displayName},` : 'Hi,';
  const count = alerts.length;
  const subject = `Judgemind Alert Digest — ${digestDate} (${count} alert${count === 1 ? '' : 's'})`;

  // Plain text version
  const alertTexts = alerts.map(renderAlertItemText).join('\n\n');
  const text = `${greeting}

You have ${count} new alert${count === 1 ? '' : 's'} from Judgemind for ${digestDate}:

${alertTexts}

View your alerts and manage subscriptions at https://judgemind.org/alerts

— The Judgemind Team`;

  // HTML version
  const alertRows = alerts.map(renderAlertItemHtml).join('');
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${subject}</title>
</head>
<body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
  <p>${greeting}</p>
  <p>You have <strong>${count}</strong> new alert${count === 1 ? '' : 's'} from <strong>Judgemind</strong> for ${digestDate}:</p>
  <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
    ${alertRows}
  </table>
  <p style="text-align: center; margin: 24px 0;">
    <a href="https://judgemind.org/alerts"
       style="background-color: #1a56db; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 4px; display: inline-block; font-weight: bold;">
      View Alerts
    </a>
  </p>
  <p style="color: #555; font-size: 13px;">You can manage your alert subscriptions from your Judgemind account settings.</p>
  <p>— The Judgemind Team</p>
</body>
</html>`;

  return { subject, html, text };
}
