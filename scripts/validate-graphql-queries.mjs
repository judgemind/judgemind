#!/usr/bin/env node
/**
 * validate-graphql-queries.mjs — Validate frontend GraphQL queries against the API schema.
 *
 * Extracts gql tagged template literals from packages/web/src/ and validates
 * each query document against the schema defined in packages/api/src/graphql/schema.ts.
 *
 * Uses the `graphql` npm package for proper schema parsing and query validation.
 * Does not require running the API server.
 *
 * Usage:
 *   node scripts/validate-graphql-queries.mjs [repo-root]
 *
 * Exit codes:
 *   0 — All queries are valid.
 *   1 — One or more validation errors found.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { buildSchema, parse, validate } from 'graphql';

const repoRoot = process.argv[2] || process.cwd();
const schemaFile = join(repoRoot, 'packages/api/src/graphql/schema.ts');
const graphqlDir = join(repoRoot, 'packages/api/src/graphql');
const webSrcDir = join(repoRoot, 'packages/web/src');

// ---------------------------------------------------------------------------
// 1. Extract and build the GraphQL schema
// ---------------------------------------------------------------------------

/**
 * Extract every `#graphql-tagged template literal from a file. The main
 * schema file (packages/api/src/graphql/schema.ts) contains the core
 * Query / Mutation types; per-module schema files under subdirectories
 * (e.g. `dispatcher/schema.ts`) extend them via `extend type Query`.
 * Returning an array lets the caller concatenate them all into a single
 * SDL string for validation.
 */
function extractAllSchemaSDL(filePath) {
  const content = readFileSync(filePath, 'utf-8');
  const startMarker = '`#graphql\n';
  const results = [];
  let cursor = 0;
  while (cursor < content.length) {
    const startIdx = content.indexOf(startMarker, cursor);
    if (startIdx === -1) break;
    const sdlStart = startIdx + startMarker.length;

    // Scan for the closing backtick, skipping escaped ones.
    let i = sdlStart;
    while (i < content.length) {
      if (content[i] === '\\' && i + 1 < content.length && content[i + 1] === '`') {
        i += 2; // skip escaped backtick
      } else if (content[i] === '`') {
        break; // found unescaped closing backtick
      } else {
        i++;
      }
    }
    if (i >= content.length) {
      throw new Error(`Could not find closing backtick for schema in ${filePath}`);
    }
    const rawSDL = content.slice(sdlStart, i);
    results.push(rawSDL.replace(/\\`/g, '`'));
    cursor = i + 1;
  }
  return results;
}

/** Back-compat wrapper — returns the first SDL block. Preserves the
 * original behaviour for callers (tests) that assume a single block. */
function extractSchemaSDL(filePath) {
  const blocks = extractAllSchemaSDL(filePath);
  if (blocks.length === 0) {
    throw new Error(`Could not find #graphql template literal in ${filePath}`);
  }
  return blocks[0];
}

/** Collect SDL blocks from all module `schema.ts` files under the graphql/
 * tree. Modules (e.g. dispatcher) declare their SDL alongside their
 * resolvers; concatenating them gives the full schema the running API
 * serves. */
function collectModuleSchemaSDLs(baseDir) {
  const blocks = [];
  function walk(dir) {
    for (const entry of readdirSync(dir)) {
      const fullPath = join(dir, entry);
      const stat = statSync(fullPath);
      if (stat.isDirectory()) {
        walk(fullPath);
      } else if (entry === 'schema.ts') {
        // Skip the top-level schema file — the caller handles it
        // explicitly so the module schemas can be appended after it.
        if (fullPath === schemaFile) continue;
        try {
          blocks.push(...extractAllSchemaSDL(fullPath));
        } catch (_err) {
          // Module schema files without a #graphql template literal are
          // tolerated (e.g. a helper file that happened to be named
          // schema.ts).
        }
      }
    }
  }
  walk(baseDir);
  return blocks;
}

// ---------------------------------------------------------------------------
// 2. Find all .ts/.tsx files, excluding __tests__/ directories
// ---------------------------------------------------------------------------

function findSourceFiles(dir) {
  const results = [];

  function walk(currentDir) {
    for (const entry of readdirSync(currentDir)) {
      const fullPath = join(currentDir, entry);
      const stat = statSync(fullPath);

      if (stat.isDirectory()) {
        // Skip __tests__ directories and node_modules
        if (entry === '__tests__' || entry === 'node_modules') continue;
        walk(fullPath);
      } else if (entry.endsWith('.ts') || entry.endsWith('.tsx')) {
        results.push(fullPath);
      }
    }
  }

  walk(dir);
  return results;
}

// ---------------------------------------------------------------------------
// 3. Extract gql tagged template literals from a file
// ---------------------------------------------------------------------------

function extractGqlQueries(filePath) {
  const content = readFileSync(filePath, 'utf-8');
  const queries = [];

  // Match gql`...` tagged template literals.
  // The gql tag can appear as: gql`, or gql `, with optional whitespace.
  const gqlRegex = /gql\s*`([\s\S]*?)`/g;
  let match;

  while ((match = gqlRegex.exec(content)) !== null) {
    const queryText = match[1].trim();
    if (!queryText) continue;

    // Skip dynamic queries that contain template expressions (${...}).
    // These are constructed at runtime and cannot be statically validated.
    if (/\$\{/.test(queryText)) continue;

    // Find the line number of this match
    const upToMatch = content.slice(0, match.index);
    const lineNumber = upToMatch.split('\n').length;
    queries.push({ text: queryText, line: lineNumber, file: filePath });
  }

  return queries;
}

// ---------------------------------------------------------------------------
// 4. Validate each query against the schema
// ---------------------------------------------------------------------------

function main() {
  // Build the schema. Start with the core SDL block, then append per-module
  // SDL blocks (e.g. `packages/api/src/graphql/dispatcher/schema.ts`) so
  // `extend type Query` / `extend type Mutation` declarations in those
  // modules are part of the schema the validator sees. This mirrors what
  // `typeDefs` in `schema.ts` actually exports at runtime.
  let schema;
  try {
    const coreBlocks = extractAllSchemaSDL(schemaFile);
    const moduleBlocks = collectModuleSchemaSDLs(graphqlDir);
    const sdl = [...coreBlocks, ...moduleBlocks].join('\n\n');
    schema = buildSchema(sdl);
  } catch (err) {
    console.error(`ERROR: Failed to build GraphQL schema: ${err.message}`);
    process.exit(1);
  }

  // Find all source files
  const sourceFiles = findSourceFiles(webSrcDir);

  // Extract and validate queries
  let totalQueries = 0;
  let totalErrors = 0;
  const errorsByFile = [];

  for (const filePath of sourceFiles) {
    const queries = extractGqlQueries(filePath);
    if (queries.length === 0) continue;

    for (const query of queries) {
      totalQueries++;
      try {
        const document = parse(query.text);
        const errors = validate(schema, document);
        if (errors.length > 0) {
          totalErrors += errors.length;
          const relPath = relative(repoRoot, filePath);
          for (const error of errors) {
            errorsByFile.push({
              file: relPath,
              line: query.line,
              message: error.message,
            });
          }
        }
      } catch (parseErr) {
        totalErrors++;
        const relPath = relative(repoRoot, filePath);
        errorsByFile.push({
          file: relPath,
          line: query.line,
          message: `Parse error: ${parseErr.message}`,
        });
      }
    }
  }

  // Report results
  if (errorsByFile.length > 0) {
    console.error(
      'ERROR: Frontend GraphQL queries reference fields not in the API schema.\n'
    );
    console.error(
      '  The following queries in packages/web/src/ are invalid against'
    );
    console.error(
      '  the schema defined in packages/api/src/graphql/schema.ts:\n'
    );

    for (const err of errorsByFile) {
      console.error(`  ${err.file}:${err.line}`);
      console.error(`    ${err.message}\n`);
    }

    console.error(
      `  Found ${totalErrors} error(s) in ${errorsByFile.length} location(s) across ${totalQueries} queries.\n`
    );
    console.error(
      '  Fix: Update the frontend query to match the API schema, or add'
    );
    console.error('  the missing field to the API schema.\n');
    process.exit(1);
  }

  console.log(
    `All clean — ${totalQueries} GraphQL queries validated against the schema.`
  );
  process.exit(0);
}

main();
