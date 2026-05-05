/**
 * Ambient module declaration for `graphql-query-complexity/cjs`.
 *
 * #4112 — we explicitly import the CJS subpath so the library uses
 * `require('graphql')` and shares the same graphql realm as Apollo
 * Server v4 (which is itself CJS). The package exposes the CJS build
 * via the `./cjs` subpath in its `exports` field, but TypeScript's
 * `moduleResolution: 'node'` (the default for `module: 'commonjs'`)
 * does not honour `exports` — it only consults `main`/`types`. The
 * runtime resolves `graphql-query-complexity/cjs` to
 * `dist/cjs/index.js` via Node 12+ subpath exports; this declaration
 * is the type-only mirror of that runtime resolution, re-exporting
 * the package's published types from their actual on-disk location.
 *
 * If we ever bump `moduleResolution` to `node16`/`nodenext`/`bundler`
 * (which DO honour `exports`), this file becomes redundant and can be
 * deleted.
 */

declare module 'graphql-query-complexity/cjs' {
  export * from 'graphql-query-complexity';
}
