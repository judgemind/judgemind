# Judgemind scraper-framework container image.
#
# Runs any registered scraper via the runner entrypoint.
# Playwright Chromium is included for JS-rendered court sites (~400 MB layer).
#
# Build:  docker build -t judgemind/scraper-framework .
# Run:    docker run -e JUDGEMIND_ARCHIVE_BUCKET=my-bucket judgemind/scraper-framework
# Single: docker run judgemind/scraper-framework ca-la-tentatives-civil

FROM python:3.12-slim AS base

# System deps required by Playwright Chromium and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
        libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for runtime security
RUN groupadd --gid 1000 scraper \
    && useradd --uid 1000 --gid scraper --create-home scraper

WORKDIR /app

# Install shared judgemind-config package first (must be available
# before pip resolves scraper-framework's dependencies).
# Build context is the repo root (docker build -f packages/scraper-framework/Dockerfile .)
COPY packages/judgemind-config /tmp/judgemind-config
RUN pip install --no-cache-dir /tmp/judgemind-config && rm -rf /tmp/judgemind-config

# Copy source before installing so hatchling can find the packages
COPY packages/scraper-framework/pyproject.toml ./
COPY packages/scraper-framework/src/ ./src/
RUN pip install --no-cache-dir . \
    && pip uninstall -y google-generativeai 2>/dev/null || true
# Install Playwright and its Chromium browser into a shared path so that
# both root (build-time) and the non-root scraper user (runtime) resolve
# the same browser binaries.  Without this, `playwright install` writes
# to /root/.cache/ms-playwright/ but the runtime USER looks in
# /home/scraper/.cache/ms-playwright/ and fails with "Executable doesn't
# exist" (see #1625).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN pip install --no-cache-dir playwright \
    && playwright install chromium --with-deps \
    && chmod -R o+rx /opt/playwright-browsers

# Fail the build if the conflicting old Google AI SDK crept back in.
# google-generativeai and google-genai share the google.genai namespace;
# having both installed breaks client.models.generate_content().
RUN ! pip show google-generativeai >/dev/null 2>&1

# Own everything by the non-root user
RUN chown -R scraper:scraper /app

USER scraper

# Base entrypoint: runs a Python module specified by CMD.
# The scraper runner uses CMD ["framework"] (default).
# The ingestion worker overrides CMD to ["ingestion"] via ECS task definition.
ENTRYPOINT ["python", "-m"]
CMD ["framework"]
