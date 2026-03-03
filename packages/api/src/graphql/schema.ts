export const typeDefs = `#graphql
  """A court jurisdiction. One row per court."""
  type Court {
    id: ID!
    """Two-letter state code, e.g. "CA"."""
    state: String!
    county: String!
    courtName: String!
    """URL-safe slug used in S3 key paths, e.g. "ca-la"."""
    courtCode: String!
    timezone: String!
    isActive: Boolean!
  }

  """A judge at a specific court. Canonical record after entity resolution."""
  type Judge {
    id: ID!
    """Normalized canonical name, e.g. "Johnson, Robert M."."""
    canonicalName: String!
    department: String
    isActive: Boolean!
    """Date of appointment, ISO 8601."""
    appointedAt: String
    court: Court
  }

  """A court case. case_number + court_id is the unique key."""
  type Case {
    id: ID!
    caseNumber: String!
    caseTitle: String
    """e.g. "civil", "criminal", "family", "probate"."""
    caseType: String
    """e.g. "active", "closed", "dismissed"."""
    caseStatus: String
    filedAt: String
    court: Court
    judges: [Judge!]!
    parties: [Party!]!
  }

  """A tentative or final ruling. isTentative=true is the primary data type."""
  type Ruling {
    id: ID!
    """ISO 8601 date of the scheduled hearing."""
    hearingDate: String!
    """One of: granted, denied, granted_in_part, denied_in_part, moot, continued, off_calendar, submitted, other."""
    outcome: String
    """e.g. "msj", "mtd", "mil", "demurrer"."""
    motionType: String
    """True for tentative rulings; false for final orders."""
    isTentative: Boolean!
    """AI-generated one-paragraph summary, pre-computed at ingestion."""
    summary: String
    department: String
    rulingText: String
    """When the court published this tentative, if known. ISO 8601."""
    postedAt: String
    case: Case
    judge: Judge
    court: Court
  }

  """A captured document — ruling HTML, motion PDF, etc. S3 object is the source of truth."""
  type Document {
    id: ID!
    """e.g. "ruling", "motion", "brief", "docket_entry", "order"."""
    documentType: String!
    """e.g. "msj", "mtd", "mil", "demurrer"."""
    motionType: String
    """Full S3 key: /{state}/{county}/{court}/{case_id}/..."""
    s3Key: String!
    s3Bucket: String!
    """One of: html, pdf, docx, txt."""
    format: String!
    """SHA-256 of raw content. Used for dedup and version detection."""
    contentHash: String!
    sourceUrl: String
    scraperId: String
    capturedAt: String!
    hearingDate: String
    """One of: active, superseded, removed."""
    status: String!
    court: Court
    case: Case
  }

  """A party (plaintiff, defendant, etc.) in a case. Canonical after entity resolution."""
  type Party {
    id: ID!
    canonicalName: String!
    """e.g. "individual", "corporation", "government"."""
    partyType: String
  }

  # ---------------------------------------------------------------------------
  # Pagination — keyset (cursor-based) for all list queries
  # ---------------------------------------------------------------------------

  type PageInfo {
    hasNextPage: Boolean!
    """Opaque cursor — pass as \`after\` to fetch the next page."""
    endCursor: String
  }

  type CaseEdge {
    node: Case!
    cursor: String!
  }

  type CaseConnection {
    edges: [CaseEdge!]!
    pageInfo: PageInfo!
  }

  type JudgeEdge {
    node: Judge!
    cursor: String!
  }

  type JudgeConnection {
    edges: [JudgeEdge!]!
    pageInfo: PageInfo!
  }

  type RulingEdge {
    node: Ruling!
    cursor: String!
  }

  type RulingConnection {
    edges: [RulingEdge!]!
    pageInfo: PageInfo!
  }

  # ---------------------------------------------------------------------------
  # Queries
  # ---------------------------------------------------------------------------

  type Query {
    health: String!

    """Fetch a single case by ID."""
    case(id: ID!): Case

    """List cases. Ordered by created_at DESC, id DESC."""
    cases(
      courtId: ID
      caseStatus: String
      caseType: String
      """Max results (default 20, max 100)."""
      first: Int
      """Opaque cursor from a previous response's pageInfo.endCursor."""
      after: String
    ): CaseConnection!

    """Fetch a single judge by ID."""
    judge(id: ID!): Judge

    """List judges. Ordered by canonical_name ASC, id ASC."""
    judges(
      courtId: ID
      first: Int
      after: String
    ): JudgeConnection!

    """Fetch a single ruling by ID."""
    ruling(id: ID!): Ruling

    """List rulings. Ordered by hearing_date DESC, id DESC."""
    rulings(
      judgeId: ID
      caseId: ID
      courtId: ID
      """Filter by county name, e.g. "Los Angeles"."""
      county: String
      """Filter by outcome, e.g. "granted"."""
      outcome: String
      """Hearings on or after this date (ISO 8601), e.g. "2026-03-01"."""
      dateFrom: String
      """Hearings on or before this date (ISO 8601), e.g. "2026-03-31"."""
      dateTo: String
      """Exact match on the linked case's case_number."""
      caseNumber: String
      first: Int
      after: String
    ): RulingConnection!
  }
`;
