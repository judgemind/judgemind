export const typeDefs = `#graphql
  type Court {
    id: ID!
    state: String!
    county: String!
    courtName: String!
    courtCode: String!
    timezone: String!
    isActive: Boolean!
  }

  type Judge {
    id: ID!
    canonicalName: String!
    department: String
    isActive: Boolean!
    appointedAt: String
    court: Court
  }

  type Case {
    id: ID!
    caseNumber: String!
    caseTitle: String
    caseType: String
    caseStatus: String
    filedAt: String
    court: Court
    judges: [Judge!]!
  }

  type Ruling {
    id: ID!
    hearingDate: String!
    outcome: String
    motionType: String
    isTentative: Boolean!
    summary: String
    department: String
    rulingText: String
    case: Case
    judge: Judge
  }

  type Query {
    health: String!

    case(id: ID!): Case
    cases(courtId: ID, caseStatus: String, first: Int, offset: Int): [Case!]!

    judge(id: ID!): Judge
    judges(courtId: ID, first: Int, offset: Int): [Judge!]!

    ruling(id: ID!): Ruling
    rulings(judgeId: ID, caseId: ID, outcome: String, first: Int, offset: Int): [Ruling!]!
  }
`;
