import Fastify, { type FastifyInstance } from 'fastify';
import { ApolloServer, HeaderMap } from '@apollo/server';
import type { Pool } from 'pg';
import type { Client } from '@opensearch-project/opensearch';
import cors from '@fastify/cors';
import swagger from '@fastify/swagger';
import swaggerUi from '@fastify/swagger-ui';
import { typeDefs } from './graphql/schema';
import { resolvers } from './graphql/resolvers';
import { createLoaders } from './graphql/dataloader';
import { pool as defaultPool } from './data-access/db';
import { extractUser } from './auth';
import { opensearchClient as defaultOsClient } from './search/client';
import { restApiPlugin } from './rest';

export async function buildApp(db?: Pool, os?: Client): Promise<FastifyInstance> {
  const pool = db ?? defaultPool;
  const opensearch = os ?? defaultOsClient;

  const app = Fastify({
    logger: process.env.NODE_ENV !== 'test',
  });

  // ---------------------------------------------------------------------------
  // CORS — allow all origins for the public API.
  // Restrict via CORS_ORIGIN env var in production if needed.
  // ---------------------------------------------------------------------------
  await app.register(cors, {
    origin: process.env.CORS_ORIGIN ?? true,
    methods: ['GET', 'POST', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'X-API-Key'],
  });

  // ---------------------------------------------------------------------------
  // OpenAPI 3.0 spec — auto-generated from route schemas.
  // ---------------------------------------------------------------------------
  await app.register(swagger, {
    openapi: {
      openapi: '3.0.0',
      info: {
        title: 'Judgemind Public API',
        description:
          'Public REST API for accessing California tentative rulings, cases, judges, attorneys, and documents. ' +
          'Unauthenticated requests are limited to 30 req/min. ' +
          'Obtain an API key from your account settings for 500 req/min.',
        version: '1.0.0',
        contact: { email: 'api@judgemind.org' },
        license: { name: 'Apache 2.0', url: 'https://www.apache.org/licenses/LICENSE-2.0' },
      },
      servers: [{ url: '/v1', description: 'API v1' }],
      components: {
        securitySchemes: {
          apiKey: {
            type: 'apiKey',
            in: 'header',
            name: 'X-API-Key',
            description: 'API key from your Judgemind account settings',
          },
        },
      },
      tags: [
        { name: 'Cases', description: 'Court cases' },
        { name: 'Judges', description: 'Judges' },
        { name: 'Attorneys', description: 'Attorneys' },
        { name: 'Documents', description: 'Captured court documents' },
        { name: 'Rulings', description: 'Tentative and final rulings' },
      ],
    },
  });

  // Swagger UI served at /docs
  await app.register(swaggerUi, {
    routePrefix: '/docs',
    uiConfig: { docExpansion: 'list', deepLinking: true },
    staticCSP: true,
  });

  // ---------------------------------------------------------------------------
  // Health check
  // ---------------------------------------------------------------------------
  app.get('/health', async (_req, reply) => {
    try {
      await pool.query('SELECT 1');
      return reply.send({ status: 'ok', db: 'connected' });
    } catch {
      return reply.status(503).send({ status: 'error', db: 'disconnected' });
    }
  });

  // ---------------------------------------------------------------------------
  // GraphQL endpoint (existing)
  // ---------------------------------------------------------------------------
  const apollo = new ApolloServer({
    typeDefs,
    resolvers,
    introspection: process.env.NODE_ENV !== 'production',
  });
  await apollo.start();

  app.addHook('onClose', async () => {
    await apollo.stop();
    if (!db) await pool.end();
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
        context: async () => {
          const user = await extractUser(req, pool);
          const ip = req.ip ?? req.headers['x-forwarded-for'] ?? 'unknown';
          const cookieHeader =
            typeof req.headers.cookie === 'string' ? req.headers.cookie : '';
          return { pool, loaders: createLoaders(pool), user, ip, reply, cookieHeader, opensearch };
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

  // ---------------------------------------------------------------------------
  // REST API v1 — scoped under /v1 prefix
  // ---------------------------------------------------------------------------
  await app.register(restApiPlugin, { prefix: '/v1', pool });

  return app;
}
