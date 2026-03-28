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
const webSrcDir = join(repoRoot, 'packages/web/src');

// ---------------------------------------------------------------------------
// 1. Extract and build the GraphQL schema
// ---------------------------------------------------------------------------

function extractSchemaSDL(filePath) {
  const content = readFileSync(filePath, 'utf-8');
  // The schema is exported as: export const typeDefs = `#graphql ... `;
  // The template literal may contain escaped backticks (\`) in description
  // strings. We need to match the full template literal including those.
  // Strategy: find the opening `#graphql marker, then scan forward,
  // skipping escaped backticks (\`) until we hit an unescaped backtick.
  const startMarker = '`#graphql\n';
  const startIdx = content.indexOf(startMarker);
  if (startIdx === -1) {
    throw new Error(`Could not find #graphql template literal in ${filePath}`);
  }
  const sdlStart = startIdx + startMarker.length;

  // Scan for the closing backtick, skipping escaped ones
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

  // Extract the SDL and unescape backticks
  const rawSDL = content.slice(sdlStart, i);
  return rawSDL.replace(/\\`/g, '`');
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
  // Build the schema
  let schema;
  try {
    const sdl = extractSchemaSDL(schemaFile);
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
