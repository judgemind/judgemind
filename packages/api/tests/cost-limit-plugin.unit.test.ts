/**
 * Direct unit tests for the error-mapping branch in `costLimitPlugin`'s
 * `didResolveOperation` hook (#4694). The HTTP-level behavior (variable
 * coercion errors → 400 BAD_USER_INPUT) is covered end-to-end in
 * `apollo-http-transport.unit.test.ts`; this file pins the non-GraphQL
 * error path, which must propagate unchanged (a bug in the estimator is a
 * server error, not a client input error).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GraphQLError, buildSchema, parse } from 'graphql';

const getComplexity = vi.fn();
vi.mock('graphql-query-complexity/cjs', () => ({
  getComplexity: (...args: unknown[]) => getComplexity(...args),
}));

const { costLimitPlugin } = await import('../src/graphql/cost-limit-plugin');

const schema = buildSchema('type Query { hello: String }');
const document = parse('{ hello }');

async function runDidResolveOperation(): Promise<void> {
  const plugin = costLimitPlugin({ maximumCost: 1000 });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const listener = await (plugin.requestDidStart as any)({});
  await listener.didResolveOperation({
    schema,
    document,
    operationName: null,
    request: { variables: {} },
  });
}

describe('costLimitPlugin error mapping', () => {
  beforeEach(() => {
    getComplexity.mockReset();
  });

  it('maps a GraphQLError from getComplexity to BAD_USER_INPUT with http 400', async () => { // status-assertion-noqa: plugin hook, no HTTP boundary
    getComplexity.mockImplementation(() => {
      throw new GraphQLError('Variable "$id" got invalid value');
    });
    const err = await runDidResolveOperation().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(GraphQLError);
    const gqlErr = err as GraphQLError;
    expect(gqlErr.message).toBe('Variable "$id" got invalid value');
    expect(gqlErr.extensions.code).toBe('BAD_USER_INPUT');
    expect(gqlErr.extensions.http).toEqual({ status: 400 });
  });

  it('re-throws non-GraphQL errors unchanged', async () => {
    const boom = new TypeError('estimator bug');
    getComplexity.mockImplementation(() => {
      throw boom;
    });
    await expect(runDidResolveOperation()).rejects.toBe(boom);
  });

  it('allows the operation when cost is under the cap', async () => {
    getComplexity.mockReturnValue(5);
    await expect(runDidResolveOperation()).resolves.toBeUndefined();
  });
});
