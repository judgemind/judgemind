/**
 * ESLint rule: prefer-path-alias
 *
 * Flags relative imports in `src/app/` files that traverse two or more
 * parent directories (`../../` or deeper). These imports break when
 * route groups are reorganised because the nesting depth changes.
 *
 * The preferred pattern is the `@/` path alias (mapped to `./src/`
 * in tsconfig.json), which is stable regardless of directory depth.
 *
 * The rule provides an auto-fix that replaces the relative path with
 * the equivalent `@/` alias.
 *
 * @see https://github.com/judgemind/judgemind/issues/1528
 * @see https://github.com/judgemind/judgemind/issues/1447
 */

'use strict';

const path = require('path');

/**
 * Count the number of leading `../` segments in an import source.
 */
function countParentTraversals(importSource) {
  let count = 0;
  let remaining = importSource;
  while (remaining.startsWith('../')) {
    count++;
    remaining = remaining.slice(3);
  }
  return count;
}

/**
 * Resolve a relative import to a `@/`-prefixed alias path, anchored
 * to the project's own `src/` directory.
 *
 * Given a file at `/project/src/app/(main)/cases/[id]/page.tsx`
 * importing `../../../../lib/display-helpers`, this resolves to
 * `@/lib/display-helpers`.
 *
 * Returns `null` if the resolved path does not fall under the
 * project's `src/` directory (prevents false positives from
 * external paths that happen to contain a `src/` directory).
 */
function resolveToAlias(importSource, currentFilePath, projectSrc) {
  const dir = path.dirname(currentFilePath);
  const resolved = path.resolve(dir, importSource);

  // Ensure the resolved path is inside the project's src directory
  if (!resolved.startsWith(projectSrc + path.sep) && resolved !== projectSrc) {
    return null;
  }

  // Calculate the alias path relative to the project's src directory
  const aliasPath = path.relative(projectSrc, resolved).split(path.sep).join('/');
  return '@/' + aliasPath;
}

/** @type {import('eslint').Rule.RuleModule} */
module.exports = {
  meta: {
    type: 'suggestion',
    fixable: 'code',
    docs: {
      description:
        'Prefer @/ path alias over deep relative imports (../../+) in app directory files. ' +
        'Deep relative imports break when route groups are reorganised.',
      recommended: false,
    },
    messages: {
      preferPathAlias:
        "Use '@/{{alias}}' instead of '{{source}}'. " +
        'Deep relative imports break when route groups are reorganised. ' +
        'See: https://github.com/judgemind/judgemind/issues/1528',
    },
    schema: [],
  },

  create(context) {
    const filename = context.getFilename();

    // Anchor to the project's src/ directory using ESLint's cwd
    // (next lint sets cwd to the packages/web/ directory)
    const projectSrc = path.join(context.getCwd(), 'src');
    const projectApp = path.join(projectSrc, 'app');

    // Only apply to files under src/app/
    if (!filename.startsWith(projectApp + path.sep)) {
      return {};
    }

    return {
      ImportDeclaration(node) {
        const source = node.source.value;

        // Only check relative imports with 2+ parent traversals
        if (typeof source !== 'string' || countParentTraversals(source) < 2) {
          return;
        }

        const alias = resolveToAlias(source, filename, projectSrc);
        if (!alias) {
          return;
        }

        // Strip the leading @/ for the message template
        const aliasPath = alias.slice(2);

        context.report({
          node: node.source,
          messageId: 'preferPathAlias',
          data: { alias: aliasPath, source },
          fix(fixer) {
            // Replace the string literal value, preserving the quote style
            const raw = node.source.raw;
            const quote = raw[0]; // ' or "
            return fixer.replaceText(node.source, quote + alias + quote);
          },
        });
      },
    };
  },
};
