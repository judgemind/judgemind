/**
 * ESLint rule: no-ssr-incompatible-import
 *
 * Prevents direct imports of packages known to be incompatible with
 * Next.js server-side rendering (SSR) in the Vercel serverless
 * environment. These packages typically depend on native binaries
 * (e.g. jsdom) that are unavailable during SSR.
 *
 * The canonical example is `isomorphic-dompurify` — its jsdom
 * dependency crashes in Vercel's serverless functions. The approved
 * pattern is to use the `@/lib/sanitize-html` utility, which defers
 * the import behind a `typeof window` guard.
 *
 * @see https://github.com/judgemind/judgemind/issues/1129
 * @see https://github.com/judgemind/judgemind/issues/1111
 */

'use strict';

/**
 * Map of SSR-incompatible package names to their suggested alternatives.
 * Add new entries here as additional problematic packages are discovered.
 */
const SSR_INCOMPATIBLE_PACKAGES = {
  'isomorphic-dompurify':
    'Use the @/lib/sanitize-html utility instead, which defers the import to avoid SSR crashes.',
};

/** @type {import('eslint').Rule.RuleModule} */
module.exports = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Disallow direct imports of SSR-incompatible packages in app components. ' +
        'These packages crash during Next.js server-side rendering on Vercel.',
      recommended: false,
    },
    messages: {
      noSsrIncompatibleImport:
        "Do not import '{{source}}' directly — it is incompatible with Next.js SSR " +
        'and will crash in the Vercel serverless environment. {{suggestion}}',
    },
    schema: [],
  },

  create(context) {
    /**
     * Report if the given source string is an SSR-incompatible package.
     */
    function check(node, source) {
      if (typeof source !== 'string') return;
      const suggestion = SSR_INCOMPATIBLE_PACKAGES[source];
      if (suggestion) {
        context.report({
          node,
          messageId: 'noSsrIncompatibleImport',
          data: { source, suggestion },
        });
      }
    }

    return {
      ImportDeclaration(node) {
        check(node, node.source.value);
      },
      CallExpression(node) {
        if (
          node.callee.type === 'Identifier' &&
          node.callee.name === 'require' &&
          node.arguments.length === 1 &&
          node.arguments[0].type === 'Literal'
        ) {
          check(node, node.arguments[0].value);
        }
      },
    };
  },
};
