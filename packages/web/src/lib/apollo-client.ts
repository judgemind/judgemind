import {
  ApolloClient,
  ApolloLink,
  InMemoryCache,
  HttpLink,
  Observable,
  gql,
} from '@apollo/client';
import { onError } from '@apollo/client/link/error';
import { getAccessToken, setAccessToken, clearAccessToken } from './auth-tokens';

const GRAPHQL_URI =
  process.env.NEXT_PUBLIC_GRAPHQL_URL ?? 'http://localhost:3001/graphql';

const REFRESH_TOKEN_MUTATION = gql`
  mutation RefreshToken {
    refreshToken {
      accessToken
    }
  }
`;

/** Whether a token refresh is already in flight. */
let isRefreshing = false;
/** Queued observers waiting for the refresh to complete. */
let pendingRequests: Array<{
  resolve: (token: string | null) => void;
  reject: (err: unknown) => void;
}> = [];

function resolvePendingRequests(token: string | null): void {
  pendingRequests.forEach(({ resolve }) => resolve(token));
  pendingRequests = [];
}

function rejectPendingRequests(err: unknown): void {
  pendingRequests.forEach(({ reject }) => reject(err));
  pendingRequests = [];
}

/**
 * Auth link: injects `Authorization: Bearer <token>` on every request
 * when an access token is available.
 */
const authLink = new ApolloLink((operation, forward) => {
  const token = getAccessToken();
  if (token) {
    operation.setContext({
      headers: {
        authorization: `Bearer ${token}`,
      },
    });
  }
  return forward(operation);
});

/**
 * Error link: intercepts UNAUTHENTICATED errors and attempts a token
 * refresh via the `refreshToken` mutation (which uses the HTTP-only
 * cookie). If the refresh succeeds, the original request is retried
 * with the new access token.
 */
function createErrorLink(client: ApolloClient<unknown>) {
  return onError(({ graphQLErrors, operation, forward }) => {
    const unauthError = graphQLErrors?.find(
      (e) => e.extensions?.code === 'UNAUTHENTICATED',
    );

    if (!unauthError) return;

    // Don't try to refresh if the failing operation IS the refresh mutation
    // or the logout mutation (avoids infinite loops).
    const opName = operation.operationName;
    if (opName === 'RefreshToken' || opName === 'Logout') {
      clearAccessToken();
      return;
    }

    // If a refresh is already in flight, queue this request
    if (isRefreshing) {
      return new Observable((observer) => {
        pendingRequests.push({
          resolve: (token) => {
            if (token) {
              operation.setContext({
                headers: { authorization: `Bearer ${token}` },
              });
            }
            forward(operation).subscribe(observer);
          },
          reject: (err) => {
            observer.error(err);
          },
        });
      });
    }

    isRefreshing = true;

    return new Observable((observer) => {
      client
        .mutate<{ refreshToken: { accessToken: string } }>({
          mutation: REFRESH_TOKEN_MUTATION,
        })
        .then(({ data }) => {
          const newToken = data?.refreshToken?.accessToken ?? null;
          setAccessToken(newToken);
          isRefreshing = false;
          resolvePendingRequests(newToken);

          // Retry the original operation with the new token
          if (newToken) {
            operation.setContext({
              headers: { authorization: `Bearer ${newToken}` },
            });
          }
          forward(operation).subscribe(observer);
        })
        .catch((err) => {
          isRefreshing = false;
          clearAccessToken();
          rejectPendingRequests(err);
          observer.error(err);
        });
    });
  });
}

export function createApolloClient(): ApolloClient<unknown> {
  const httpLink = new HttpLink({
    uri: GRAPHQL_URI,
    credentials: 'include',
  });

  const client = new ApolloClient({
    // errorLink needs the client reference for the refresh mutation, so we
    // build it after constructing the client with a placeholder link, then
    // swap in the full chain.
    link: ApolloLink.from([authLink, httpLink]),
    cache: new InMemoryCache({
      typePolicies: {
        // DataQualityOverview has no `id` field — without explicit keyFields
        // Apollo cannot distinguish array items and may collapse them into a
        // single cache entry, causing the dashboard to render only one county.
        DataQualityOverview: {
          keyFields: ['county'],
        },
      },
    }),
  });

  // Now that we have the client, create the error link and rebuild the chain.
  const errorLink = createErrorLink(client);
  client.setLink(ApolloLink.from([errorLink, authLink, httpLink]));

  return client;
}
