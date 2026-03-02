export const typeDefs = `#graphql
  type Query {
    case(id: ID!): Case
    cases(limit: Int = 20, offset: Int = 0): [Case!]!
    judge(id: ID!): Judge
    judges(limit: Int = 20, offset: Int = 0): [Judge!]!
    ruling(id: ID!): Ruling
    rulings(
      judgeId: ID
      caseId: ID
      limit: Int = 20
      offset: Int = 0
    ): [Ruling!]!
  }

  type Case {
    id: ID!
    caseNumber: String!
    caseTitle: String
    caseType: String
    caseStatus: String
    filedAt: String
    judges: [Judge!]!
    rulings: [Ruling!]!
  }

  type Judge {
    id: ID!
    canonicalName: String!
    department: String
    isActive: Boolean!
  }

  type Ruling {
    id: ID!
    outcome: String
    motionType: String
    hearingDate: String!
    isTentative: Boolean!
    summary: String
    rulingText: String
    postedAt: String
    case: Case
    judge: Judge
  }
`;
