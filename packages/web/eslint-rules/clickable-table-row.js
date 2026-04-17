/**
 * ESLint rule: clickable-table-row
 *
 * Flags `<TableRow>` elements that contain a nested `<Link>` (or `<a>`) but
 * no `onClick` handler on the row itself. The shadcn `TableRow` component
 * applies a default `hover:bg-muted/50` highlight that makes every row
 * *look* clickable on hover, even when only a nested link is navigable.
 * This mismatched affordance has produced the same UX bug three separate
 * times (cases, rulings, judges) before this rule existed.
 *
 * The approved pattern — documented in `docs/web-patterns.md` §Clickable
 * Table Rows — is:
 *
 *   <TableRow
 *     className="cursor-pointer"
 *     onClick={() => router.push(`/detail/${id}`)}
 *   >
 *     <TableCell onClick={(e) => e.stopPropagation()}>
 *       <Checkbox ... />
 *     </TableCell>
 *     <TableCell>
 *       <Link href={...} onClick={(e) => e.stopPropagation()}>...</Link>
 *     </TableCell>
 *   </TableRow>
 *
 * The row itself navigates; nested interactive elements (checkboxes, buttons,
 * the link itself for right-click / cmd-click affordance) stop propagation.
 *
 * Header rows (the `<TableRow>` inside `<TableHeader>`) are NOT expected to
 * be clickable. The rule detects header rows by looking at their cell
 * children: a row containing only `<TableHead>` children is treated as a
 * header row and skipped.
 *
 * @see https://github.com/judgemind/judgemind/issues/2156
 */

'use strict';

/**
 * Extract the element name from a JSX opening element.
 * Returns the full dotted name (e.g. "foo.Bar") for member expressions.
 */
function getElementName(openingElement) {
  const name = openingElement.name;
  if (name.type === 'JSXIdentifier') {
    return name.name;
  }
  if (name.type === 'JSXMemberExpression') {
    const parts = [];
    let current = name;
    while (current.type === 'JSXMemberExpression') {
      parts.unshift(current.property.name);
      current = current.object;
    }
    if (current.type === 'JSXIdentifier') {
      parts.unshift(current.name);
    }
    return parts.join('.');
  }
  return null;
}

/**
 * Check whether a JSX element has an attribute with the given name, either
 * as a named attribute (`onClick={...}`) or via JSX spread (`{...props}`).
 *
 * Spread attributes may contain `onClick` at runtime — we cannot statically
 * determine this. To avoid false positives for reasonable patterns like
 * `<TableRow {...rowProps}>`, treat any spread attribute as potentially
 * providing `onClick`.
 */
function hasAttributeOrSpread(openingElement, attrName) {
  for (const attr of openingElement.attributes) {
    if (attr.type === 'JSXSpreadAttribute') {
      return true;
    }
    if (
      attr.type === 'JSXAttribute' &&
      attr.name.type === 'JSXIdentifier' &&
      attr.name.name === attrName
    ) {
      return true;
    }
  }
  return false;
}

/**
 * Walk all JSX descendants of a node and invoke the visitor for each
 * JSXElement encountered. Stops descending into a subtree if the visitor
 * returns the sentinel value `SKIP_SUBTREE`.
 */
const SKIP_SUBTREE = Symbol('SKIP_SUBTREE');

function walkJsxChildren(node, visit) {
  if (!node || !node.children) return;
  for (const child of node.children) {
    if (child.type === 'JSXElement') {
      const result = visit(child);
      if (result !== SKIP_SUBTREE) {
        walkJsxChildren(child, visit);
      }
    } else if (child.type === 'JSXFragment') {
      walkJsxChildren(child, visit);
    } else if (child.type === 'JSXExpressionContainer') {
      // Descend into expression containers to find JSX inside map callbacks,
      // ternaries, logical expressions, etc.
      walkExpression(child.expression, visit);
    }
  }
}

function walkExpression(expr, visit) {
  if (!expr || typeof expr !== 'object') return;

  if (expr.type === 'JSXElement') {
    const result = visit(expr);
    if (result !== SKIP_SUBTREE) {
      walkJsxChildren(expr, visit);
    }
    return;
  }
  if (expr.type === 'JSXFragment') {
    walkJsxChildren(expr, visit);
    return;
  }

  // Traverse common expression shapes that can contain JSX.
  const childKeys = [
    'consequent', 'alternate', 'body', 'argument', 'left', 'right',
    'expression', 'expressions', 'elements', 'callee', 'arguments',
    'object', 'property', 'value',
  ];
  for (const key of childKeys) {
    const child = expr[key];
    if (Array.isArray(child)) {
      for (const item of child) {
        walkExpression(item, visit);
      }
    } else if (child && typeof child === 'object' && child.type) {
      walkExpression(child, visit);
    }
  }
}

/**
 * Determine whether a TableRow is a header row. Heuristic: a header row
 * contains `<TableHead>` children but no `<TableCell>` children. We only
 * inspect direct children (not descendants) so a data row with a nested
 * header-like element is not accidentally skipped.
 */
function isHeaderRow(tableRowElement) {
  let sawTableHead = false;
  let sawTableCell = false;
  for (const child of tableRowElement.children) {
    if (child.type !== 'JSXElement') continue;
    const name = getElementName(child.openingElement);
    if (name === 'TableHead') sawTableHead = true;
    if (name === 'TableCell') sawTableCell = true;
  }
  return sawTableHead && !sawTableCell;
}

/** @type {import('eslint').Rule.RuleModule} */
module.exports = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Enforce that <TableRow> elements containing a nested <Link> or <a> ' +
        'also have an onClick handler. Rows look clickable on hover but are ' +
        'not navigable unless the row itself handles the click.',
      recommended: false,
    },
    messages: {
      missingOnClick:
        '<TableRow> contains a nested <{{linkName}}> but has no onClick handler. ' +
        'The row appears clickable due to the default hover highlight, but only ' +
        'the inner link is navigable — a misleading affordance. Add ' +
        'onClick={() => router.push(...)} with cursor-pointer on the row, and ' +
        'onClick={(e) => e.stopPropagation()} on the nested <{{linkName}}>. ' +
        'See docs/web-patterns.md §Clickable Table Rows. ' +
        'Tracked in https://github.com/judgemind/judgemind/issues/2156',
    },
    schema: [],
  },

  create(context) {
    return {
      JSXElement(node) {
        const name = getElementName(node.openingElement);
        if (name !== 'TableRow') {
          return;
        }

        // Header rows are not expected to navigate — skip them.
        if (isHeaderRow(node)) {
          return;
        }

        // If the row already has onClick (or a spread that might provide it),
        // the affordance is correct.
        if (hasAttributeOrSpread(node.openingElement, 'onClick')) {
          return;
        }

        // Search descendants for a <Link> or <a> element. Stop descending
        // into any nested <TableRow> — those are a separate concern and
        // will be visited on their own by the JSXElement visitor.
        let foundLinkName = null;
        walkJsxChildren(node, (descendant) => {
          if (foundLinkName) return SKIP_SUBTREE;
          const descName = getElementName(descendant.openingElement);
          if (descName === 'TableRow') {
            return SKIP_SUBTREE;
          }
          if (descName === 'Link' || descName === 'a') {
            foundLinkName = descName;
            return SKIP_SUBTREE;
          }
          return undefined;
        });

        if (foundLinkName) {
          context.report({
            node: node.openingElement,
            messageId: 'missingOnClick',
            data: { linkName: foundLinkName },
          });
        }
      },
    };
  },
};
