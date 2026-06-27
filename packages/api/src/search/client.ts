import { Client } from '@opensearch-project/opensearch';
import { AwsSigv4Signer } from '@opensearch-project/opensearch/aws-v3';
import { defaultProvider } from '@aws-sdk/credential-provider-node';

const DEFAULT_REGION = 'us-west-2';

type ClientOptions = ConstructorParameters<typeof Client>[0];

// SigV4-signed requests are the preferred, deployed auth path: the client
// signs requests with the ambient AWS credential chain so the OpenSearch
// domain's resource-based access policy can re-tighten to enumerated role ARNs
// instead of Principal = "*" (#4040). HTTP Basic auth via OpenSearch FGAC's
// internal user database remains as the local-dev fallback for the
// Docker-Compose OpenSearch, which does not speak SigV4 (see #3771 for the
// access-policy root-cause history).
export function buildOpensearchClientOptions(
  env: NodeJS.ProcessEnv = process.env,
): ClientOptions {
  const node = env.OPENSEARCH_URL ?? 'http://localhost:9200';
  const username = env.OPENSEARCH_USERNAME ?? '';
  const password = env.OPENSEARCH_PASSWORD ?? '';
  const region = env.AWS_REGION ?? env.AWS_DEFAULT_REGION ?? DEFAULT_REGION;

  const baseOptions: ClientOptions = {
    node,
    ssl: { rejectUnauthorized: false },
  };

  if (username && password) {
    // local-dev fallback: HTTP Basic auth against the Docker-Compose
    // OpenSearch (which does not speak SigV4). Gated behind explicit env vars.
    return {
      ...baseOptions,
      auth: { username, password },
    };
  }

  // No no-auth fallback here (unlike the Python make_opensearch_client helper)
  // is intentional (#4040). The API service only ever runs in two
  // configurations: (1) deployed on ECS, where the task role always supplies
  // AWS credentials, so SigV4 resolves at the first request; (2) local dev,
  // where OPENSEARCH_USERNAME/OPENSEARCH_PASSWORD are set per
  // docs/agent/local-dev.md, so the basic-auth branch above is taken.
  // AwsSigv4Signer's getCredentials is lazy (resolved per-request, not at
  // construction), so building the signer never throws at module load — there
  // is no import-time regression even when credentials are absent. The Python
  // helper's no-auth path exists for a different consumer: rebuild_db.py /
  // reingest_from_s3.py can run locally with OPENSEARCH_URL set but no creds at
  // all. That scenario does not apply to the always-credentialed API service.
  const signer = AwsSigv4Signer({
    region,
    service: 'es',
    getCredentials: () => defaultProvider()(),
  });

  return {
    ...signer,
    ...baseOptions,
  };
}

export const opensearchClient = new Client(buildOpensearchClientOptions());
