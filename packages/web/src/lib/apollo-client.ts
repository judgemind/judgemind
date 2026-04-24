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
        // ---------------------------------------------------------------------
        // Types without `id` that appear in arrays — keyFields required to
        // prevent Apollo from collapsing distinct items into a single cache
        // entry (see #1542, #1762).
        // ---------------------------------------------------------------------

        DataQualityOverview: {
          keyFields: ['county'],
        },
        OutcomeCount: {
          keyFields: ['outcome'],
        },
        MotionStats: {
          keyFields: ['motionType'],
        },

        // JudgeAssignment is embedded within the Judge type as an array.
        // Uses keyFields: false since department alone isn't unique (a judge
        // could theoretically return to the same department).
        JudgeAssignment: { keyFields: false },

        // RulingSearchHit uses `rulingId` instead of `id` as its unique key.
        RulingSearchHit: {
          keyFields: ['rulingId'],
        },
        // JudgeAnalytics is a single-response type but uses `judgeId` instead
        // of `id`. Explicit keyFields lets Apollo cache analytics for multiple
        // judges without collisions.
        JudgeAnalytics: {
          keyFields: ['judgeId'],
        },

        // ---------------------------------------------------------------------
        // Edge types — keyFields: false disables normalization so they stay
        // embedded in their parent connection rather than being individually
        // cached. Edges have no natural unique key (cursor is positional, not
        // identity-based).
        // ---------------------------------------------------------------------

        RulingSearchEdge: { keyFields: false },
        CaseEdge: { keyFields: false },
        JudgeEdge: { keyFields: false },
        RulingEdge: { keyFields: false },
        DataQualityMetricEdge: { keyFields: false },

        // ---------------------------------------------------------------------
        // Dispatcher admin types (#2730, #2805). DispatcherAgent uses `id`,
        // so it auto-normalizes — no keyFields needed. The rest lack `id`:
        //
        // - DispatcherRun: unique by `runId` (single-response in DispatcherState).
        // - DispatcherFailure: unique by `failureId` (appears inside arrays).
        // - PhaseTransition: unique by `transitionId` (appears inside arrays).
        // - DispatcherCommandResult: unique by `commandId` (mutation result).
        // - DispatcherState: singleton root object (no arrays of it); opt out.
        // - QueueItem: unique by `issueNumber` (#2805 §1.3).
        // - RecentCompletion: unique by `agentId` (#2805 §1.5).
        // - DispatcherConfigEntry: unique by `key` (#2805 §1.6).
        // ---------------------------------------------------------------------

        DispatcherRun: {
          keyFields: ['runId'],
        },
        DispatcherFailure: {
          keyFields: ['failureId'],
        },
        PhaseTransition: {
          keyFields: ['transitionId'],
        },
        DispatcherCommandResult: {
          keyFields: ['commandId'],
        },
        DispatcherState: { keyFields: false },
        QueueItem: {
          keyFields: ['issueNumber'],
        },
        RecentCompletion: {
          keyFields: ['agentId'],
        },
        DispatcherConfigEntry: {
          keyFields: ['key'],
        },
        // DispatcherQueueFull: full-list payload for the cockpit's
        // expand-count dialog (issue #3159). One cache entry per kind
        // bucket — `keyFields: ['kind']` keeps READY / BLOCKED /
        // COMPLETED separate so opening a dialog after another doesn't
        // get the previous bucket's cached payload.
        DispatcherQueueFull: {
          keyFields: ['kind'],
        },

        // ---------------------------------------------------------------------
        // Types that intentionally do NOT need keyFields:
        //
        // - AuthPayload: single response from login/register, not in arrays.
        // - PlatformStats: single response from platformStats query.
        // - PageInfo: embedded single object within each connection.
        // - *Connection types (RulingSearchConnection, CaseConnection,
        //   JudgeConnection, RulingConnection, DataQualityMetricConnection):
        //   connection wrappers, not array items themselves.
        // - DispatcherAgent: has `id`, auto-normalizes.
        // ---------------------------------------------------------------------
      },
    }),
  });

  // Now that we have the client, create the error link and rebuild the chain.
  const errorLink = createErrorLink(client);
  client.setLink(ApolloLink.from([errorLink, authLink, httpLink]));

  return client;
}
