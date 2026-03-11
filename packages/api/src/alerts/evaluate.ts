import type { Pool } from 'pg';

type Row = Record<string, unknown>;

interface AlertFilters {
  judge_id?: string;
  case_id?: string;
  keyword?: string;
  party_id?: string;
}

interface MatchedEvent {
  subscription_id: string;
  ruling_id: string;
  document_id: string | null;
}

/**
 * Evaluate all active alert subscriptions against a newly indexed ruling.
 * Inserts matching `alert_events` rows for subscriptions that match.
 *
 * @param pool - PostgreSQL connection pool
 * @param rulingId - The ID of the ruling to evaluate alerts against
 * @returns The number of alert events created
 */
export async function evaluateAlerts(pool: Pool, rulingId: string): Promise<number> {
  // Load the ruling with related data
  const { rows: rulingRows } = await pool.query<Row>(
    `SELECT r.id, r.judge_id, r.case_id, r.court_id, r.document_id, r.ruling_text
     FROM rulings r
     WHERE r.id = $1`,
    [rulingId],
  );

  if (rulingRows.length === 0) {
    return 0;
  }

  const ruling = rulingRows[0];
  const rulingJudgeId = ruling.judge_id as string | null;
  const rulingCaseId = ruling.case_id as string;
  const rulingText = (ruling.ruling_text as string | null) ?? '';
  const rulingDocumentId = ruling.document_id as string | null;

  // Load party IDs for the ruling's case
  const { rows: partyRows } = await pool.query<Row>(
    `SELECT party_id FROM case_parties WHERE case_id = $1`,
    [rulingCaseId],
  );
  const casePartyIds = new Set(partyRows.map((r) => r.party_id as string));

  // Load all active subscriptions
  const { rows: subscriptions } = await pool.query<Row>(
    `SELECT id, alert_type, filters FROM alert_subscriptions WHERE is_active = true`,
  );

  const matchedEvents: MatchedEvent[] = [];

  for (const sub of subscriptions) {
    const subId = sub.id as string;
    const alertType = sub.alert_type as string;
    const filters = sub.filters as AlertFilters;

    let matched = false;

    switch (alertType) {
      case 'judge_ruling':
        matched = !!rulingJudgeId && filters.judge_id === rulingJudgeId;
        break;

      case 'case_docket':
        matched = !!filters.case_id && filters.case_id === rulingCaseId;
        break;

      case 'keyword':
        if (filters.keyword && rulingText) {
          matched = rulingText.toLowerCase().includes(filters.keyword.toLowerCase());
        }
        break;

      case 'party_attorney':
        matched = !!filters.party_id && casePartyIds.has(filters.party_id);
        break;
    }

    if (matched) {
      matchedEvents.push({
        subscription_id: subId,
        ruling_id: rulingId,
        document_id: rulingDocumentId,
      });
    }
  }

  if (matchedEvents.length === 0) {
    return 0;
  }

  // Batch insert all matched events
  const values: unknown[] = [];
  const placeholders: string[] = [];
  let paramIdx = 1;

  for (const event of matchedEvents) {
    placeholders.push(`($${paramIdx++}, $${paramIdx++}, $${paramIdx++})`);
    values.push(event.subscription_id, event.ruling_id, event.document_id);
  }

  await pool.query(
    `INSERT INTO alert_events (subscription_id, ruling_id, document_id)
     VALUES ${placeholders.join(', ')}`,
    values,
  );

  return matchedEvents.length;
}
