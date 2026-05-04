import { Client } from '@opensearch-project/opensearch';

const OPENSEARCH_URL = process.env.OPENSEARCH_URL ?? 'http://localhost:9200';
const OPENSEARCH_USERNAME = process.env.OPENSEARCH_USERNAME ?? '';
const OPENSEARCH_PASSWORD = process.env.OPENSEARCH_PASSWORD ?? '';

const clientOptions: ConstructorParameters<typeof Client>[0] = {
  node: OPENSEARCH_URL,
  ssl: { rejectUnauthorized: false },
};

// HTTP Basic auth via OpenSearch FGAC's internal user database. The
// domain's resource-based access policy MUST permit Principal = "*" for
// basic-auth requests to be evaluated by FGAC at all — see #3771 for the
// full root-cause analysis of what happens when the policy is narrowed.
// A future migration to SigV4-signed requests would let the access policy
// re-tighten to enumerated role ARNs (#3704); track that follow-up issue
// (filed alongside #3771) before changing the policy in
// `infra/terraform/modules/search/main.tf`.
if (OPENSEARCH_USERNAME && OPENSEARCH_PASSWORD) {
  clientOptions.auth = {
    username: OPENSEARCH_USERNAME,
    password: OPENSEARCH_PASSWORD,
  };
}

export const opensearchClient = new Client(clientOptions);
