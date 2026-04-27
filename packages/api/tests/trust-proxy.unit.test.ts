/**
 * Unit test: Fastify trustProxy resolves req.ip from X-Forwarded-For header.
 *
 * Verifies AC1 of issue #3643: `trustProxy: true` is correctly set so that
 * Fastify resolves `req.ip` from the XFF header injected by the ALB.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';

let app: FastifyInstance;

beforeAll(async () => {
  app = await buildApp();

  // Register a test-only route to echo req.ip back to the caller.
  // We register it after buildApp so it doesn't pollute production routes.
  app.get('/__test/echo-ip', async (req) => {
    return { ip: req.ip };
  });
}, 10_000);

afterAll(async () => {
  await app?.close();
});

describe('trustProxy — req.ip resolution from X-Forwarded-For', () => {
  it('resolves req.ip to the XFF client IP (1.2.3.4) when trustProxy is true', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/__test/echo-ip',
      headers: {
        'x-forwarded-for': '1.2.3.4',
      },
    });

    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body) as { ip: string };
    expect(body.ip).toBe('1.2.3.4');
  });

  it('resolves req.ip to the first IP in a multi-hop XFF chain', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/__test/echo-ip',
      headers: {
        'x-forwarded-for': '9.8.7.6, 10.0.0.1',
      },
    });

    expect(res.statusCode).toBe(200);
    const body = JSON.parse(res.body) as { ip: string };
    // Fastify with trustProxy: true resolves to the leftmost (client) IP
    expect(body.ip).toBe('9.8.7.6');
  });
});
