/**
 * ESLint rule: polled-query-error-guard
 *
 * Flags `useQuery(..., { pollInterval })` callsites where the destructured
 * `error` is used in a bare `if (error) return <JSX/>` short-circuit.
 *
 * Why this matters: Apollo's `useQuery` with `pollInterval` will return a
 * stale `data` value alongside a new `error` on subsequent poll failures.
 * A bare `if (error) return <ErrorBanner/>` will hide the previously-loaded
 * data, making the UI appear broken even though the last successful response
 * is still available. The correct guard is `if (error && !data)` — show the
 * error only when there is no data to fall back to.
 *
 * The approved pattern — documented in `docs/web-patterns.md` §Apollo
 * Polling — is:
 *
 *   const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
 *   if (error && !data) return <ErrorBanner error={error} />;
 *
 * Invalid:
 *
 *   const { data, loading, error } = useQuery(QUERY, { pollInterval: 5000 });
 *   if (error) return <ErrorBanner error={error} />;
 *
 * @see https://github.com/judgemind/judgemind/issues/2850
 */

'use strict';

/**
 * Check whether an ObjectExpression contains a property with the given key
 * name as a shorthand, identifier-valued, or computed property.
 */
function hasPropertyKey(objectExpression, keyName) {
  if (!objectExpression || objectExpression.type !== 'ObjectExpression') return false;
  for (const prop of objectExpression.properties) {
    if (prop.type !== 'Property') continue;
    if (prop.key && prop.key.type === 'Identifier' && prop.key.name === keyName) {
      return true;
    }
  }
  return false;
}

/**
 * Given an ObjectPattern (destructuring), extract the local binding name for
 * a given source key. Handles renaming: `{ error: queryError }` returns
 * `'queryError'` for key `'error'`. Returns null if the key is not present.
 */
function getDestructuredBinding(objectPattern, keyName) {
  if (!objectPattern || objectPattern.type !== 'ObjectPattern') return null;
  for (const prop of objectPattern.properties) {
    if (prop.type !== 'Property') continue;
    if (!prop.key || prop.key.type !== 'Identifier') continue;
    if (prop.key.name !== keyName) continue;
    // The local name is in the `value` (which may be renamed or an identifier).
    if (prop.value && prop.value.type === 'Identifier') {
      return prop.value.name;
    }
  }
  return null;
}

/**
 * Walk the statements in a function body (BlockStatement) for IfStatement
 * nodes. Also descends into nested blocks, but NOT into nested function
 * definitions (to avoid false positives from callbacks).
 */
function collectIfStatements(bodyNode, results) {
  if (!bodyNode) return;
  if (bodyNode.type === 'BlockStatement') {
    for (const stmt of bodyNode.body) {
      collectIfStatements(stmt, results);
    }
    return;
  }
  if (bodyNode.type === 'IfStatement') {
    results.push(bodyNode);
    // Also walk the branches for chained ifs.
    collectIfStatements(bodyNode.consequent, results);
    collectIfStatements(bodyNode.alternate, results);
    return;
  }
  // Descend into try/catch, loops, etc. but not into nested functions.
  if (
    bodyNode.type === 'TryStatement' ||
    bodyNode.type === 'WhileStatement' ||
    bodyNode.type === 'ForStatement' ||
    bodyNode.type === 'ForInStatement' ||
    bodyNode.type === 'ForOfStatement'
  ) {
    if (bodyNode.block) collectIfStatements(bodyNode.block, results);
    if (bodyNode.handler && bodyNode.handler.body) {
      collectIfStatements(bodyNode.handler.body, results);
    }
    if (bodyNode.finalizer) collectIfStatements(bodyNode.finalizer, results);
    if (bodyNode.body) collectIfStatements(bodyNode.body, results);
  }
}

/**
 * Check whether a statement (or block) contains a JSX return — i.e., a
 * `return` whose argument is a JSXElement or JSXFragment.
 */
function consequentReturnsJsx(node) {
  if (!node) return false;
  // Direct return statement: `if (error) return <Foo/>;`
  if (node.type === 'ReturnStatement') {
    return isJsx(node.argument);
  }
  // Block: `if (error) { return <Foo/>; }`
  if (node.type === 'BlockStatement') {
    for (const stmt of node.body) {
      if (stmt.type === 'ReturnStatement' && isJsx(stmt.argument)) {
        return true;
      }
    }
  }
  // Expression statement with JSX (unusual but possible).
  return false;
}

function isJsx(node) {
  if (!node) return false;
  return node.type === 'JSXElement' || node.type === 'JSXFragment';
}

/**
 * Find the nearest enclosing function body for a given node using the
 * ESLint ancestor stack (context.getAncestors()).
 */
function getEnclosingFunctionBody(ancestors) {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const anc = ancestors[i];
    if (
      anc.type === 'FunctionDeclaration' ||
      anc.type === 'FunctionExpression' ||
      anc.type === 'ArrowFunctionExpression'
    ) {
      return anc.body;
    }
  }
  return null;
}

/** @type {import('eslint').Rule.RuleModule} */
module.exports = {
  meta: {
    type: 'problem',
    fixable: 'code',
    docs: {
      description:
        'Enforce that useQuery calls with pollInterval guard the error short-circuit as ' +
        '`if (error && !data)` rather than bare `if (error)`. Polled queries retain ' +
        'stale data after a poll failure; a bare `if (error)` discards it.',
      recommended: false,
    },
    messages: {
      bareErrorGuard:
        'Polled useQuery: bare `if ({{errorBinding}})` hides stale data on poll failures. ' +
        'Use `if ({{errorBinding}} && !{{dataBinding}})` instead. ' +
        'See docs/web-patterns.md §Apollo Polling. ' +
        'Tracked in https://github.com/judgemind/judgemind/issues/2850',
      bareErrorGuardNoData:
        'Polled useQuery: bare `if ({{errorBinding}})` hides stale data on poll failures. ' +
        'Add a `data` binding and use `if ({{errorBinding}} && !data)` instead. ' +
        'See docs/web-patterns.md §Apollo Polling. ' +
        'Tracked in https://github.com/judgemind/judgemind/issues/2850',
    },
    schema: [],
  },

  create(context) {
    return {
      VariableDeclarator(node) {
        // Must be: const { ... } = useQuery(QUERY, { pollInterval: ... })
        const init = node.init;
        if (!init || init.type !== 'CallExpression') return;

        const callee = init.callee;
        if (!callee || callee.type !== 'Identifier' || callee.name !== 'useQuery') return;

        // Must have a second argument that is an object with pollInterval.
        const args = init.arguments;
        if (!args || args.length < 2) return;
        const options = args[1];
        if (!hasPropertyKey(options, 'pollInterval')) return;

        // The declarator left side must be an ObjectPattern (destructuring).
        const id = node.id;
        if (!id || id.type !== 'ObjectPattern') return;

        const errorBinding = getDestructuredBinding(id, 'error');
        if (!errorBinding) return; // Not destructuring `error` — nothing to check.

        const dataBinding = getDestructuredBinding(id, 'data');

        // Walk the enclosing function body for bare IfStatements.
        const ancestors = context.getAncestors();
        const funcBody = getEnclosingFunctionBody(ancestors);
        if (!funcBody) return;

        const ifStatements = [];
        collectIfStatements(funcBody, ifStatements);

        for (const ifStmt of ifStatements) {
          const test = ifStmt.test;

          // Already guarded with a logical expression (e.g. `error && !data`,
          // `error || loading`) — skip.
          if (test.type === 'LogicalExpression') continue;

          // Check for bare `if (errorBinding)`.
          if (test.type !== 'Identifier') continue;
          if (test.name !== errorBinding) continue;

          // Consequent must return JSX.
          if (!consequentReturnsJsx(ifStmt.consequent)) continue;

          if (dataBinding) {
            context.report({
              node: ifStmt.test,
              messageId: 'bareErrorGuard',
              data: { errorBinding, dataBinding },
              fix(fixer) {
                return fixer.replaceText(
                  ifStmt.test,
                  `${errorBinding} && !${dataBinding}`,
                );
              },
            });
          } else {
            context.report({
              node: ifStmt.test,
              messageId: 'bareErrorGuardNoData',
              data: { errorBinding },
            });
          }
        }
      },
    };
  },
};
