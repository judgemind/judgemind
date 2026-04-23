/**
 * ESLint rule: no-client-utility-import
 *
 * Prevents server components from importing non-component (utility) exports
 * from 'use client' modules. In Next.js App Router, server components can
 * render client components via their exports, but non-component exports
 * (utility functions, constants) are stripped during SSR serialization,
 * causing runtime errors.
 *
 * Allowed:
 *   - Default imports from 'use client' modules (typically the main component)
 *   - PascalCase named imports (React component convention)
 *
 * Disallowed:
 *   - camelCase or UPPER_CASE named imports from 'use client' modules
 *     into files that do NOT have 'use client'
 *
 * @see https://github.com/judgemind/judgemind/issues/643
 */

'use strict';

const fs = require('fs');
const path = require('path');

/**
 * Check if an import name looks like a React component (PascalCase).
 * PascalCase starts with an uppercase letter followed by at least one
 * lowercase letter (e.g. MyComponent, CaseDetail). This excludes
 * ALL_CAPS constants (e.g. FORMAT_LABELS, SOME_CONSTANT) which are
 * utility exports, not components.
 */
function isPascalCase(name) {
  return /^[A-Z][a-zA-Z0-9]*[a-z]/.test(name);
}

/**
 * Resolve an import source to an absolute file path.
 * Handles relative imports only — package imports are ignored.
 * Tries common extensions: .tsx, .ts, .jsx, .js, and /index variants.
 */
function resolveImportPath(importSource, currentFilePath) {
  if (!importSource.startsWith('.')) {
    return null; // Not a relative import
  }

  const dir = path.dirname(currentFilePath);
  const base = path.resolve(dir, importSource);

  // Try direct file with extensions
  const extensions = ['.tsx', '.ts', '.jsx', '.js'];
  for (const ext of extensions) {
    const candidate = base + ext;
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  // Try index files
  for (const ext of extensions) {
    const candidate = path.join(base, 'index' + ext);
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  // If the path already has an extension, try it directly
  if (fs.existsSync(base)) {
    return base;
  }

  return null;
}

/**
 * Check if a file starts with 'use client' directive.
 * Reads only the first few bytes for efficiency.
 */
function hasUseClientDirective(filePath) {
  try {
    // Read enough bytes to check for the directive
    const fd = fs.openSync(filePath, 'r');
    const buffer = Buffer.alloc(64);
    const bytesRead = fs.readSync(fd, buffer, 0, 64, 0);
    fs.closeSync(fd);
    const header = buffer.toString('utf8', 0, bytesRead).trimStart();
    return header.startsWith("'use client'") || header.startsWith('"use client"');
  } catch {
    return false;
  }
}

/** @type {import('eslint').Rule.RuleModule} */
module.exports = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Disallow importing non-component exports from "use client" modules into server components',
      recommended: false,
    },
    messages: {
      noClientUtilityImport:
        "'{{name}}' is a non-component export from a 'use client' module ({{source}}). " +
        'Move it to a shared module without "use client" so server components can import it safely. ' +
        'See: https://github.com/judgemind/judgemind/issues/643',
    },
    schema: [],
  },

  create(context) {
    const filename = context.getFilename();

    // Skip if the current file is a 'use client' module — client-to-client
    // imports are fine.
    if (hasUseClientDirective(filename)) {
      return {};
    }

    return {
      ImportDeclaration(node) {
        const source = node.source.value;

        // Only check relative imports
        if (!source.startsWith('.')) {
          return;
        }

        // Resolve the imported file path
        const resolvedPath = resolveImportPath(source, filename);
        if (!resolvedPath) {
          return;
        }

        // Check if the imported file is a 'use client' module
        if (!hasUseClientDirective(resolvedPath)) {
          return;
        }

        // Check each imported specifier
        for (const specifier of node.specifiers) {
          // Default imports are always safe (typically the main component)
          if (specifier.type === 'ImportDefaultSpecifier') {
            continue;
          }

          // Namespace imports (import * as X) from client modules are risky
          if (specifier.type === 'ImportNamespaceSpecifier') {
            context.report({
              node: specifier,
              messageId: 'noClientUtilityImport',
              data: {
                name: '* as ' + specifier.local.name,
                source,
              },
            });
            continue;
          }

          // Named imports — check if PascalCase (component convention)
          const importedName =
            specifier.imported.type === 'Identifier'
              ? specifier.imported.name
              : specifier.imported.value;

          if (!isPascalCase(importedName)) {
            context.report({
              node: specifier,
              messageId: 'noClientUtilityImport',
              data: {
                name: importedName,
                source,
              },
            });
          }
        }
      },
    };
  },
};
