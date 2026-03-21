# Web Frontend — Lessons Learned

Common issues found during audits and fixes. Consult this when writing or reviewing frontend code.

## Server Component Error Handling

- **Wrap ALL post-fetch processing inside try/catch blocks.** In Next.js server components, data fetching typically has its own try/catch, but any code that runs _after_ the fetch (sanitization, formatting, transformation) can also throw. If that code is outside the try/catch, the uncaught exception causes an HTTP 500 for every request — even though the error boundary catches it, the user sees a generic "Something went wrong" page instead of a graceful degradation.

  **The #1280 incident:** The ruling detail page had a try/catch around the GraphQL fetch but called `sanitizeRulingHtml()` _outside_ it. When sanitization failed in Vercel's serverless environment (jsdom unavailable), the uncaught exception caused HTTP 500 for all rulings with HTML content. It took 4 PRs to diagnose because the sanitization call was not suspected as the failure point.

  **Pattern — correct:**
  ```tsx
  // Fetch data with try/catch
  let data = null;
  try {
    const client = createApolloClient();
    const { data: result } = await client.query({ query, variables });
    data = result?.entity ?? null;
  } catch {
    // Fetch failed — fall through to not found
  }

  if (!data) notFound();

  // Post-fetch processing ALSO in try/catch with fallback
  let processedValue = defaultFallback;
  try {
    processedValue = riskyTransform(data.field);
  } catch {
    // Transform failed — use fallback value
  }
  ```

  **Pattern — risky (avoid):**
  ```tsx
  let data = null;
  try {
    const { data: result } = await client.query({ query, variables });
    data = result?.entity ?? null;
  } catch {
    // Only catches fetch errors
  }

  if (!data) notFound();

  // This is OUTSIDE the try/catch — if it throws, HTTP 500
  const processedValue = riskyTransform(data.field);
  ```

- **Pure formatting functions are generally safe** outside try/catch. Functions like `formatLabel()`, `formatDate()`, `buildCaseHeading()` that do simple string manipulation on already-validated data are unlikely to throw. The rule above primarily applies to functions that:
  - Use external libraries (e.g., DOMPurify/jsdom for HTML sanitization)
  - Parse complex input (e.g., date parsing, JSON parsing)
  - Access environment-specific APIs that may not be available in all runtimes
  - Process user-generated or externally-sourced content

- **Next.js error boundaries are a safety net, not a strategy.** The root `error.tsx` catches uncaught exceptions and shows "Something went wrong." But this is a degraded user experience — always prefer graceful fallbacks (e.g., showing plain text instead of sanitized HTML) over relying on the error boundary.

## Code Review Checklist

When reviewing server component PRs, check:

1. **Are all post-fetch function calls inside try/catch or provably safe?** Look for function calls between the data fetch's `catch` block and the `return` JSX. If any call uses external libraries, parses complex input, or could fail in serverless environments, it needs its own try/catch.

2. **Do try/catch blocks have meaningful fallbacks?** A bare `catch {}` that silently swallows errors is fine if there is a fallback value. But `catch {}` with no fallback that lets undefined propagate to JSX can cause hydration errors.

3. **Is the error boundary test adequate?** If adding a new server component page, verify that the root `error.tsx` properly catches and displays errors for that route.
