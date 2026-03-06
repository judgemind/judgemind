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

  """The authenticated user."""
  type User {
    id: ID!
    email: String!
    emailVerified: Boolean!
    displayName: String
    role: String!
    createdAt: String!
  }

  """Returned on successful authentication."""
  type AuthPayload {
    accessToken: String!
    user: User!
  }

  # ---------------------------------------------------------------------------
  # Search — full-text search over OpenSearch
  # ---------------------------------------------------------------------------

  """Filters for tentative ruling search. All fields are optional."""
  input RulingSearchFilters {
    """Exact court name, e.g. "Superior Court of California, County of Los Angeles"."""
    court: String
    """Exact county name, e.g. "Los Angeles"."""
    county: String
    """Two-letter state code, e.g. "CA"."""
    state: String
    """Exact judge name as indexed, e.g. "Crowfoot, William A."."""
    judgeName: String
    """Hearing date on or after this date (ISO 8601)."""
    dateFrom: String
    """Hearing date on or before this date (ISO 8601)."""
    dateTo: String
    """Case number prefix, e.g. "24NNCV"."""
    caseNumber: String
  }

  """A single search hit from the tentative ruling search."""
  type RulingSearchHit {
    """Ruling ID in the database — use with the ruling(id) query for full details."""
    rulingId: ID!
    caseNumber: String
    court: String
    county: String
    state: String
    judgeName: String
    hearingDate: String
    """Highlighted excerpt from the ruling text (HTML with <mark> tags)."""
    excerpt: String
    """Relevance score (only present for full-text queries)."""
    score: Float
  }

  type RulingSearchEdge {
    node: RulingSearchHit!
    cursor: String!
  }

  type RulingSearchConnection {
    edges: [RulingSearchEdge!]!
    pageInfo: PageInfo!
    """Total number of documents matching the query."""
    totalHits: Int!
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

    """Full-text + filtered search over tentative rulings via OpenSearch.
    Provide a \`query\` for full-text BM25 search, \`filters\` for metadata filtering, or both.
    An empty query with filters returns results sorted by hearing date descending.
    By default, rulings with future hearing dates are excluded. Pass includeFuture: true to include them."""
    searchRulings(
      """Free-text search query against ruling text."""
      query: String
      """Metadata filters (court, county, state, judge, dates, case number prefix)."""
      filters: RulingSearchFilters
      """Max results (default 20, max 100)."""
      first: Int
      """Opaque cursor from a previous response's pageInfo.endCursor."""
      after: String
      """Include rulings with future hearing dates (default false). For admin/debugging use."""
      includeFuture: Boolean
    ): RulingSearchConnection!

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

    """List rulings. Ordered by hearing_date DESC, id DESC.
    By default, rulings with future hearing dates are excluded. Pass includeFuture: true to include them (admin/debugging)."""
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
      """Include rulings with future hearing dates (default false). For admin/debugging use."""
      includeFuture: Boolean
      first: Int
      after: String
    ): RulingConnection!

    """Return the currently authenticated user, or null."""
    me: User
  }

  # ---------------------------------------------------------------------------
  # Mutations — authentication
  # ---------------------------------------------------------------------------

  type Mutation {
    """Register with email and password. Sends a verification email."""
    register(email: String!, password: String!, displayName: String): AuthPayload!

    """Login with email and password."""
    login(email: String!, password: String!): AuthPayload!

    """Invalidate the current refresh token."""
    logout: Boolean!

    """Exchange a valid refresh token (from httpOnly cookie) for a new access token."""
    refreshToken: AuthPayload!

    """Verify email address using the token sent during registration."""
    verifyEmail(token: String!): Boolean!

    """Begin Google OAuth: returns the redirect URL to send the user to."""
    initiateGoogleAuth: String!

    """Complete Google OAuth: exchange the authorization code for an auth payload."""
    completeGoogleAuth(code: String!): AuthPayload!
  }
`;
