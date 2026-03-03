import Fastify, { type FastifyInstance } from 'fastify';
import { ApolloServer, HeaderMap } from '@apollo/server';
import type { Pool } from 'pg';
import { typeDefs } from './graphql/schema';
import { resolvers } from './graphql/resolvers';
import { createLoaders } from './graphql/dataloader';
import { pool as defaultPool } from './data-access/db';

export async function buildApp(db?: Pool): Promise<FastifyInstance> {
  const pool = db ?? defaultPool;

  const app = Fastify({
    logger: process.env.NODE_ENV !== 'test',
  });

  const apollo = new ApolloServer({
    typeDefs,
    resolvers,
    // Introspection disabled in production to reduce attack surface.
    introspection: process.env.NODE_ENV !== 'production',
  });
  await apollo.start();

  app.addHook('onClose', async () => {
    await apollo.stop();
    // Only end the pool if we're using the module-level default; callers that
    // pass their own pool are responsible for closing it.
    if (!db) await pool.end();
  });

  app.get('/health', async (_req, reply) => {
    try {
      await pool.query('SELECT 1');
      return reply.send({ status: 'ok', db: 'connected' });
    } catch {
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
        // Fresh DataLoaders per request — never share across requests.
        context: async () => ({ pool, loaders: createLoaders(pool) }),
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
