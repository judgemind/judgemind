# Investigation Patterns

Detailed guidance for the "instrument before you guess" principle. When a failure's root cause isn't obvious, instrumentation comes before patching.

## What Agents Get Wrong

Anchoring on the first plausible hypothesis is the most common investigation mistake. An unconfirmed-guess fix burns a full cycle and often hides the real mechanism behind defensive handling. Instrumentation PRs are cheap; wrong-fix rollbacks aren't.

## How to Apply

Write instrumentation first when a failure is reproducible but the mechanism isn't clear — log what the process *actually* saw (stderr tail, response bytes, DOM state, SQL executed, last N inputs), not what you think it saw. Don't ship a fix based on a hypothesis you haven't confirmed with a captured artifact. Defensive coercion (`coerce to {} on non-dict`, swallow on parse error) is fine to prevent crashes but must not hide the underlying signal — keep the raw data on disk or in a log event.

## Applies Everywhere

This pattern applies across all subsystems:

- **Scraping** — "no records" → log what matched before concluding the page was empty.
- **NLP** — "wrong extraction" → capture model I/O before changing prompts.
- **CI flakes** — capture reproduction artifacts before adding retries.
- **Infra timeouts** — log the operation timeline before tuning limits.
- **Frontend / data-quality anomalies** — capture the raw DOM or query result before patching the rendering layer.
