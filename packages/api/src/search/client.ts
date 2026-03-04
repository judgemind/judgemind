import { Client } from '@opensearch-project/opensearch';

const OPENSEARCH_URL = process.env.OPENSEARCH_URL ?? 'http://localhost:9200';

export const opensearchClient = new Client({
  node: OPENSEARCH_URL,
  ssl: { rejectUnauthorized: false },
});
