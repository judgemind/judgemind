/**
 * Unit tests: GraphQL SDL parse check.
 *
 * Importing the SDL strings at the top of this file is the first line of
 * defence — any template-literal eval error (e.g. a double-backtick inside
 * a `#graphql` docstring) causes Vitest to fail the *file load*, before
 * `parse()` even runs, with a clear "X is not a function" error rather
 * than a silent runtime failure buried in integration test output.
 *
 * The `parse()` calls then validate the SDL shape itself.
 */

import { describe, it, expect } from 'vitest';
import { parse } from 'graphql';
import { dispatcherTypeDefs } from '../src/graphql/dispatcher/schema';
import { typeDefs } from '../src/graphql/schema';

describe('graphql SDL parse check', () => {
  it('dispatcherTypeDefs parses as valid SDL', () => {
    expect(() => parse(dispatcherTypeDefs)).not.toThrow();
  });

  it('typeDefs (core + dispatcher concatenated) parses as valid SDL', () => {
    expect(() => parse(typeDefs)).not.toThrow();
  });

  it.skip('REGRESSION: double-backtick inside docstring would fail at module load', () => {
    // If someone re-introduces a ``foo`` sequence inside a `#graphql` template
    // literal, e.g.:
    //
    //   export const dispatcherTypeDefs = `#graphql
    //     """Use ``backtick`` syntax here."""
    //     type Foo { id: ID! }
    //   `;
    //
    // The file that imports `dispatcherTypeDefs` (including this test file)
    // would fail to load at all with an error like:
    //
    //   TypeError: (intermediate value) is not a function
    //
    // because the JS engine interprets ``backtick`` as a tagged-template call
    // on the preceding string value. The error appears before parse() runs,
    // making the failure visible at CI time as a file-load error, not as a
    // silent wrong-output. Kept as .skip because an intentional-failure demo
    // cannot live in an always-running test suite.
    //
    // See issue #2948 for the original incident report and fix.
  });
});
