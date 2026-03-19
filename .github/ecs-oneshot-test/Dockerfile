# Minimal ECS oneshot test image.
#
# Simulates the scraper-framework container environment that ECS oneshot
# scripts run in.  Installs the same dependencies as the production image
# (from packages/scraper-framework/pyproject.toml) but skips Playwright
# and browser deps to keep the image small and fast to build.
#
# Build context must be the repo root so that packages/scraper-framework/
# is accessible.

FROM python:3.12-slim

# Install only the system deps needed for lxml (skip Playwright browser deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install scraper-framework deps (same as production image, minus playwright)
COPY packages/scraper-framework/pyproject.toml ./pyproject.toml
COPY packages/scraper-framework/src/ ./src/

# Install without playwright to keep the test image lean.
# Scripts that import playwright will fail at import time, which is the
# correct behavior for a oneshot script (playwright is available in the
# real container but should not be used by batch scripts).
RUN pip install --no-cache-dir . \
    && pip uninstall -y google-generativeai 2>/dev/null || true

# The entrypoint is bash -c, matching what ecs-run-task.sh uses
ENTRYPOINT ["bash", "-c"]
CMD ["echo 'No command provided'"]
