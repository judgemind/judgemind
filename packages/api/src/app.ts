import { ApolloServer, HeaderMap } from '@apollo/server';
import Fastify, { FastifyInstance } from 'fastify';
import { typeDefs } from './graphql/schema.js';
import { resolvers } from './graphql/resolvers.js';
import pool from './data-access/pool.js';

export async function buildApp(): Promise<FastifyInstance> {
  const apollo = new ApolloServer({ typeDefs, resolvers });
  await apollo.start();

  const app = Fastify({ logger: true });

  // Health check
  app.get('/health', async (_req, reply) => {
    try {
      await pool.query('SELECT 1');
      return reply.send({ status: 'ok', db: 'ok' });
    } catch {
      return reply.status(503).send({ status: 'error', db: 'unavailable' });
    }
  });

  // GraphQL endpoint
  app.route({
    url: '/graphql',
    method: ['GET', 'POST'],
    handler: async (request, reply) => {
      const headers = new HeaderMap();
      for (const [key, value] of Object.entries(request.headers)) {
        if (value !== undefined) {
          headers.set(key, Array.isArray(value) ? value.join(', ') : value);
        }
      }

      const searchIndex = request.url.indexOf('?');
      const search = searchIndex >= 0 ? request.url.slice(searchIndex) : '';

      const response = await apollo.executeHTTPGraphQLRequest({
        httpGraphQLRequest: {
          method: request.method.toUpperCase(),
          headers,
          body: request.body,
          search,
        },
        context: async () => ({}),
      });

      reply.status(response.status ?? 200);
      for (const [key, value] of response.headers) {
        reply.header(key, value);
      }

      if (response.body.kind === 'complete') {
        return reply.send(response.body.string);
      }

      // Incremental delivery (subscriptions/defer) — stream chunks
      for await (const chunk of response.body.asyncIterator) {
        reply.raw.write(chunk);
      }
      reply.raw.end();
    },
  });

  app.addHook('onClose', async () => {
    await apollo.stop();
  });

  return app;
}
