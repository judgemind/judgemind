import type { Pool } from 'pg';
import { sendEmail } from '../email/send';
import { renderDigestEmail } from '../email/templates/digest';
import type { DigestAlertItem } from '../email/templates/digest';

type Row = Record<string, unknown>;

interface UserDigest {
  userId: string;
  email: string;
  displayName: string | null;
  eventIds: string[];
  alerts: DigestAlertItem[];
}

/**
 * Send daily digest emails to all users with pending (unsent) alert events.
 *
 * Groups events by user, composes a single digest email per user, sends via SES,
 * and marks events as sent.
 *
 * @param pool - PostgreSQL connection pool
 * @returns The number of digest emails sent
 */
export async function sendAlertDigests(pool: Pool): Promise<number> {
  // Find all unsent alert events, joined with subscription user info and ruling data
  const { rows } = await pool.query<Row>(
    `SELECT
       ae.id AS event_id,
       asub.user_id,
       asub.alert_type,
       u.email,
       u.display_name,
       r.hearing_date,
       r.outcome,
       r.motion_type,
       r.ruling_text,
       c.case_number,
       c.case_title,
       j.canonical_name AS judge_name
     FROM alert_events ae
     JOIN alert_subscriptions asub ON asub.id = ae.subscription_id
     JOIN users u ON u.id = asub.user_id
     LEFT JOIN rulings r ON r.id = ae.ruling_id
     LEFT JOIN cases c ON c.id = r.case_id
     LEFT JOIN judges j ON j.id = r.judge_id
     WHERE ae.digest_sent = false
     ORDER BY asub.user_id, ae.triggered_at ASC`,
  );

  if (rows.length === 0) {
    return 0;
  }

  // Group events by user
  const userDigests = new Map<string, UserDigest>();

  for (const row of rows) {
    const userId = row.user_id as string;

    if (!userDigests.has(userId)) {
      userDigests.set(userId, {
        userId,
        email: row.email as string,
        displayName: row.display_name as string | null,
        eventIds: [],
        alerts: [],
      });
    }

    const digest = userDigests.get(userId)!;
    digest.eventIds.push(row.event_id as string);

    // Build excerpt from ruling text (first 200 chars)
    const rulingText = row.ruling_text as string | null;
    const excerpt = rulingText ? rulingText.substring(0, 200) + (rulingText.length > 200 ? '...' : '') : undefined;

    digest.alerts.push({
      alertType: row.alert_type as string,
      caseNumber: row.case_number as string | undefined,
      caseTitle: row.case_title as string | undefined,
      judgeName: row.judge_name as string | undefined,
      hearingDate: row.hearing_date ? String(row.hearing_date) : undefined,
      outcome: row.outcome as string | undefined,
      motionType: row.motion_type as string | undefined,
      excerpt,
    });
  }

  const today = new Date().toISOString().slice(0, 10);
  let emailsSent = 0;

  for (const digest of userDigests.values()) {
    const { subject, html, text } = renderDigestEmail({
      displayName: digest.displayName ?? undefined,
      alerts: digest.alerts,
      digestDate: today,
    });

    await sendEmail({
      to: digest.email,
      subject,
      htmlBody: html,
      textBody: text,
    });

    // Mark all events for this user as sent in a single batch update
    await pool.query(
      `UPDATE alert_events
       SET digest_sent = true, included_in_digest_at = NOW()
       WHERE id = ANY($1::uuid[])`,
      [digest.eventIds],
    );

    emailsSent++;
  }

  return emailsSent;
}
