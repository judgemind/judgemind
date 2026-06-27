import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the SigV4 signer + credential provider so no real AWS/network calls
// happen and we can assert which auth branch was taken. The signer returns a
// recognizable marker object spread into the client options. See #4040.
// vi.mock factories are hoisted above module-level consts, so the spy is
// declared via vi.hoisted to be available inside the factory.
const { awsSigv4Signer } = vi.hoisted(() => ({
  awsSigv4Signer: vi.fn(() => ({ Connection: 'conn', Transport: 'transport' })),
}));

vi.mock('@opensearch-project/opensearch/aws-v3', () => ({
  AwsSigv4Signer: (opts: unknown) => awsSigv4Signer(opts),
}));

vi.mock('@aws-sdk/credential-provider-node', () => ({
  defaultProvider: () => () => Promise.resolve({}),
}));

// Prevent the module-load-time `new Client(...)` from doing anything real.
vi.mock('@opensearch-project/opensearch', () => ({
  Client: class {
    constructor() {}
  },
}));

import { buildOpensearchClientOptions } from '../src/search/client';

describe('buildOpensearchClientOptions', () => {
  beforeEach(() => {
    awsSigv4Signer.mockClear();
  });

  it('uses basic auth when both username and password are set', () => {
    const opts = buildOpensearchClientOptions({
      OPENSEARCH_URL: 'http://localhost:9200',
      OPENSEARCH_USERNAME: 'admin',
      OPENSEARCH_PASSWORD: 'secret',
    } as NodeJS.ProcessEnv);

    expect(opts.auth).toEqual({ username: 'admin', password: 'secret' });
    expect(opts.node).toBe('http://localhost:9200');
    // No SigV4 signer in the basic-auth path.
    expect(awsSigv4Signer).not.toHaveBeenCalled();
    expect('Connection' in opts).toBe(false);
  });

  it('uses SigV4 signer when no basic-auth env is present', () => {
    const opts = buildOpensearchClientOptions({
      OPENSEARCH_URL: 'https://search.example.com',
    } as NodeJS.ProcessEnv);

    expect(awsSigv4Signer).toHaveBeenCalledTimes(1);
    expect(opts.auth).toBeUndefined();
    // Signer marker fields were spread into the options.
    expect((opts as Record<string, unknown>).Connection).toBe('conn');
    expect((opts as Record<string, unknown>).Transport).toBe('transport');
    expect(opts.node).toBe('https://search.example.com');
  });

  it('uses SigV4 when only one of username/password is set', () => {
    const opts = buildOpensearchClientOptions({
      OPENSEARCH_URL: 'https://search.example.com',
      OPENSEARCH_USERNAME: 'admin',
    } as NodeJS.ProcessEnv);

    expect(awsSigv4Signer).toHaveBeenCalledTimes(1);
    expect(opts.auth).toBeUndefined();
  });

  it('defaults the region to us-west-2', () => {
    buildOpensearchClientOptions({
      OPENSEARCH_URL: 'https://search.example.com',
    } as NodeJS.ProcessEnv);

    expect(awsSigv4Signer).toHaveBeenCalledWith(
      expect.objectContaining({ region: 'us-west-2', service: 'es' }),
    );
  });

  it('honors AWS_REGION over the default', () => {
    buildOpensearchClientOptions({
      OPENSEARCH_URL: 'https://search.example.com',
      AWS_REGION: 'us-east-2',
    } as NodeJS.ProcessEnv);

    expect(awsSigv4Signer).toHaveBeenCalledWith(
      expect.objectContaining({ region: 'us-east-2' }),
    );
  });

  it('falls back to AWS_DEFAULT_REGION when AWS_REGION is unset', () => {
    buildOpensearchClientOptions({
      OPENSEARCH_URL: 'https://search.example.com',
      AWS_DEFAULT_REGION: 'eu-central-1',
    } as NodeJS.ProcessEnv);

    expect(awsSigv4Signer).toHaveBeenCalledWith(
      expect.objectContaining({ region: 'eu-central-1' }),
    );
  });

  it('defaults the node URL to localhost when OPENSEARCH_URL is unset', () => {
    const opts = buildOpensearchClientOptions({} as NodeJS.ProcessEnv);
    expect(opts.node).toBe('http://localhost:9200');
  });
});
