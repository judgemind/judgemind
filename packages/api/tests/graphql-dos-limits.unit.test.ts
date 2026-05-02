import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';

let app: FastifyInstance;

beforeAll(async () => {
  app = await buildApp();
});

afterAll(async () => {
  await app.close();
});

// Generates a deeply nested GraphQL query using the Case ↔ Ruling circular
// relationship (Case.latestRuling → Ruling, Ruling.case → Case).
// graphql-depth-limit counts each object field as +1 from its parent.
// Nesting pattern: case(1) → latestRuling(2) → case(3) → latestRuling(4) → …
// Each pair of latestRuling+case adds 2 levels. For depth ≥ 11, use pairs=5:
//   case(1) → latestRuling(2) → case(3) → … → latestRuling(10) → case(11) → id(leaf)
// To trigger the maxDepth=10 limit, the reported depth must be > 10 (i.e. ≥ 11).
function buildDepthQuery(targetDepth: number): string {
  if (targetDepth <= 1)
    return '{ case(id: "00000000-0000-0000-0000-000000000000") { id } }';

  // Build innermost part: leaf scalar inside the deepest case
  let inner = 'id';
  // Each iteration wraps with latestRuling { case { … } } adding 2 depth levels.
  // Starting from depth=1 (outer case), each pair adds 2. We need targetDepth-1
  // more levels. Use Math.ceil((targetDepth - 1) / 2) pairs.
  const pairs = Math.ceil((targetDepth - 1) / 2);
  for (let i = 0; i < pairs; i++) {
    // Ruling.case is a plain field (no argument); only root Query.case takes id.
    inner = `latestRuling { case { ${inner} } }`;
  }
  return `{ case(id: "00000000-0000-0000-0000-000000000000") { ${inner} } }`;
}

describe('GraphQL DoS limits', () => {
  it('depth-11 query returns 400 with depth-limit error', async () => {
    const query = buildDepthQuery(11);
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query }),
    });
    expect(res.statusCode).toBe(400);
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
    expect(Array.isArray(body.errors)).toBe(true);
    expect(body.errors.length).toBeGreaterThan(0);
    // At least one error should mention depth
    const hasDepthError = body.errors.some(
      (e: { message: string }) =>
        typeof e.message === 'string' && e.message.toLowerCase().includes('depth'),
    );
    expect(hasDepthError).toBe(true);
  });

  it('oversized body (200KB) returns 413 Payload Too Large', async () => {
    // Pad the query to exceed 100KB
    const padding = 'x'.repeat(200_000);
    const oversizedPayload = JSON.stringify({
      query: `{ __typename }`,
      extensions: { padding },
    });
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: oversizedPayload,
    });
    expect(res.statusCode).toBe(413);
  });

  it('depth-5 legitimate query passes validation (no validation errors)', async () => {
    // Depth 5: query(1) → case(2) → judges(3) → court(4) → courtName(5)
    // Case.judges returns [Judge!]!, and Judge.court returns Court.
    const query = `{
      case(id: "00000000-0000-0000-0000-000000000000") {
        caseNumber
        court {
          courtName
        }
        judges {
          canonicalName
          court {
            state
          }
        }
        parties {
          id
        }
      }
    }`;
    const res = await app.inject({
      method: 'POST',
      url: '/graphql',
      headers: { 'content-type': 'application/json' },
      payload: JSON.stringify({ query }),
    });
    // Should not be 400 (validation error) — may be 200 with data null and resolver errors (no DB)
    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body);
    // Must not have validation errors (validation errors use 400); any errors here
    // are resolver errors (DB not available), which still return 200.
    // Specifically, there should be no depth-limit or complexity errors.
    if (body.errors) {
      const hasValidationError = body.errors.some(
        (e: { message: string; extensions?: { code?: string } }) =>
          e.message.toLowerCase().includes('depth') ||
          e.message.toLowerCase().includes('complexity') ||
          e.extensions?.code === 'GRAPHQL_VALIDATION_FAILED',
      );
      expect(hasValidationError).toBe(false);
    }
  });
});
