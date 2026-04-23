import { Client } from '@opensearch-project/opensearch';

const OPENSEARCH_URL = process.env.OPENSEARCH_URL ?? 'http://localhost:9200';
const OPENSEARCH_USERNAME = process.env.OPENSEARCH_USERNAME ?? '';
const OPENSEARCH_PASSWORD = process.env.OPENSEARCH_PASSWORD ?? '';

const clientOptions: ConstructorParameters<typeof Client>[0] = {
  node: OPENSEARCH_URL,
  ssl: { rejectUnauthorized: false },
};

if (OPENSEARCH_USERNAME && OPENSEARCH_PASSWORD) {
  clientOptions.auth = {
    username: OPENSEARCH_USERNAME,
    password: OPENSEARCH_PASSWORD,
  };
}

export const opensearchClient = new Client(clientOptions);
