import Fastify, { type FastifyInstance } from 'fastify';
import { ApolloServer, HeaderMap } from '@apollo/server';
import type { Pool } from 'pg';
import type { Client } from '@opensearch-project/opensearch';
import depthLimit from 'graphql-depth-limit';
import { createComplexityRule } from 'graphql-query-complexity';
import { judgemindEstimator } from './graphql/cost-rule-estimator';
import { typeDefs } from './graphql/schema';
import { resolvers } from './graphql/resolvers';
import { createLoaders } from './graphql/dataloader';
import { costBreakdownPlugin } from './graphql/cost-breakdown';
import { pool as defaultPool } from './data-access/db';
import { extractUser } from './auth';
import { opensearchClient as defaultOsClient } from './search/client';
import { registerDocumentDownload } from './rest/document-download';
import { registerDocumentContent } from './rest/document-content';

export async function buildApp(db?: Pool, os?: Client): Promise<FastifyInstance> {
  const pool = db ?? defaultPool;
  const opensearch = os ?? defaultOsClient;

  const app = Fastify({
    logger: process.env.NODE_ENV !== 'test',
    trustProxy: true,
    bodyLimit: 100_000,
  });

  // ── CORS ────────────────────────────────────────────────────────────────
  const allowedOrigins = (process.env.CORS_ALLOWED_ORIGINS ?? '')
    .split(',')
    .map((o) => o.trim())
    .filter(Boolean);

  if (allowedOrigins.length > 0) {
    app.addHook('onRequest', async (req, reply) => {
      const origin = req.headers.origin;
      if (origin && allowedOrigins.includes(origin)) {
        reply.header('Access-Control-Allow-Origin', origin);
        reply.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        reply.header('Access-Control-Allow-Headers', 'Content-Type, Authorization');
        reply.header('Access-Control-Allow-Credentials', 'true');
      }
      if (req.method === 'OPTIONS') {
        reply.status(204).send();
      }
    });
  }

  const apollo = new ApolloServer({
    typeDefs,
    resolvers,
    // Introspection disabled in production to reduce attack surface.
    introspection: process.env.NODE_ENV !== 'production',
    validationRules: [
      depthLimit(10),
      // #4003 cost cap: 1000 units. The custom `judgemindEstimator`
      // mirrors the now-retired prior cost-rule library's algorithm
      // (scalarCost=1, objectCost=0, listFactor=10) so the cap, the
      // production cost-vs-cap relationship, and the per-field breakdown
      // logger (`cost-breakdown.ts`) all stay in sync. Migration: #4112.
      createComplexityRule({
        maximumComplexity: 1000,
        estimators: [judgemindEstimator],
        onComplete: (cost: number) => app.log.info({ cost }, 'graphql.cost'),
      }),
    ],
    plugins: [
      // Issue #4100 — emit a per-top-level-field cost breakdown when an
      // operation's total cost is within the early-warning band of the
      // 1000-cap (≥ 800). The cost rule above only exposes the total via
      // `onCost`; this plugin walks the same algorithm and names which
      // fields dominate, so future cap-overflow incidents can be triaged
      // from a single CloudWatch line. Threshold-gated to keep log
      // volume bounded — the dispatcher polls every 2s and we don't
      // want a breakdown for every cheap query.
      costBreakdownPlugin((entry) =>
        app.log.info(entry, 'graphql.cost.breakdown'),
      ),
    ],
  });
  await apollo.start();

  app.addHook('onClose', async () => {
    await apollo.stop();
    // Only end the pool if we're using the module-level default; callers that
    // pass their own pool are responsible for closing it.
    if (!db) await pool.end();
  });

  // ── REST routes ──────────────────────────────────────────────────────────
  registerDocumentDownload(app, pool);
  registerDocumentContent(app, pool);

  app.get('/health', async (req, reply) => {
    try {
      await pool.query('SELECT 1');
      return reply.send({ status: 'ok', db: 'connected' });
    } catch (err) {
      req.log.error({ err }, 'Health check: database connection failed');
      return reply.status(503).send({ status: 'error', db: 'disconnected' });
    }
  });

  app.route({
    method: ['GET', 'POST'],
    url: '/graphql',
    handler: async (req, reply) => {
      const headers = new HeaderMap();
      for (const [key, val] of Object.entries(req.headers)) {
        if (typeof val === 'string') {
          headers.set(key, val);
        } else if (Array.isArray(val)) {
          headers.set(key, val.join(', '));
        }
      }

      const response = await apollo.executeHTTPGraphQLRequest({
        httpGraphQLRequest: {
          method: req.method.toUpperCase(),
          headers,
          body: req.body,
          search: req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '',
        },
        // Fresh DataLoaders, auth context, and OpenSearch client per request.
        context: async () => {
          const user = await extractUser(req, pool);
          const ip = req.ip ?? 'unknown';
          const cookieHeader =
            typeof req.headers.cookie === 'string' ? req.headers.cookie : '';
          // #2884: X-MFA-Token parsing removed with the MFA re-auth gate.
          // Admin surfaces rely on `user.role === 'admin'` alone; audit
          // trail lives in `dispatcher.commands.issued_by`.
          return {
            pool,
            loaders: createLoaders(pool),
            user,
            ip,
            reply,
            cookieHeader,
            opensearch,
          };
        },
      });

      reply.status(response.status ?? 200);
      for (const [key, val] of response.headers) {
        reply.header(key, val);
      }

      if (response.body.kind === 'complete') {
        return reply.send(response.body.string);
      }

      let body = '';
      for await (const chunk of response.body.asyncIterator) {
        body += chunk;
      }
      return reply.send(body);
    },
  });

  return app;
}
