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

# Copy source before installing so hatchling can find the packages
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir playwright \
    && playwright install chromium --with-deps

# Own everything by the non-root user
RUN chown -R scraper:scraper /app

USER scraper

# Base entrypoint: runs a Python module specified by CMD.
# The scraper runner uses CMD ["framework"] (default).
# The ingestion worker overrides CMD to ["ingestion"] via ECS task definition.
ENTRYPOINT ["python", "-m"]
CMD ["framework"]
