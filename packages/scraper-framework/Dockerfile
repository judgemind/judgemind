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

# Install Patchright's Chromium into the same shared path. Patchright is
# a stealth-optimized Playwright fork used by the SF civil scraper (see
# #2622); it respects ``PLAYWRIGHT_BROWSERS_PATH`` for browser lookup, so
# installing here puts its bundled Chrome channel alongside the stock
# chromium build. ``--with-deps`` is a no-op when the system libs are
# already present (they were installed for playwright above) but is
# harmless to re-run.
RUN patchright install chromium --with-deps \
    && chmod -R o+rx /opt/playwright-browsers

# Fail the build if the conflicting old Google AI SDK crept back in.
# google-generativeai and google-genai share the google.genai namespace;
# having both installed breaks client.models.generate_content().
RUN ! pip show google-generativeai >/dev/null 2>&1

# Embed data-quality baselines so ECS oneshot tasks (which only upload the
# script, not the repo) can still find the baselines file.  The build
# context is the repo root, so this file is accessible.  See #2323.
COPY data-quality-baselines.json /app/data-quality-baselines.json

# Embed the EventBridge-driven oneshot scripts that share this image as
# their runtime.  These are the scripts referenced from ECS task
# definitions wired by `infra/terraform/modules/scraper-*-check` and
# `infra/terraform/modules/scraper-field-population-audit`.  Without
# this COPY, the ECS task fails with "No such file or directory" --
# the existing zero-record-check task hits exactly that error today.
# See #4255 (this audit) and the predecessor zero-record bug.
COPY scripts/audit_scraper_field_population.py /app/scripts/audit_scraper_field_population.py
COPY scripts/check-scraper-zero-record-runner.py /app/scripts/check-scraper-zero-record-runner.py
COPY scripts/check-scraper-zero-record-streak.py /app/scripts/check-scraper-zero-record-streak.py
COPY scripts/notify-telegram.sh /app/scripts/notify-telegram.sh

# Embed the operator-only reingest/rebuild scripts that target the
# ingestion-worker image via `scripts/ecs-run-task.sh`.  These are run
# frequently for backfills, carry-forward sweeps, cache-bust reingest,
# and county-scoped rebuilds, and the ecs-run-task.sh path uploads them
# via S3 every time -- baking them into the image lets operators invoke
# them directly as `python /app/scripts/<name>.py` without the runtime
# download dance, and pins the executed code to the image build SHA
# instead of whatever happens to be at HEAD when the task fires.  The
# combined ~280 KB image-size impact is negligible.  See #4288.
COPY scripts/reingest_from_s3.py /app/scripts/reingest_from_s3.py
COPY scripts/rebuild_db.py /app/scripts/rebuild_db.py

# Own everything by the non-root user
RUN chown -R scraper:scraper /app

USER scraper

# Base entrypoint: runs a Python module specified by CMD.
# The scraper runner uses CMD ["framework"] (default).
# The ingestion worker overrides CMD to ["ingestion"] via ECS task definition.
ENTRYPOINT ["python", "-m"]
CMD ["framework"]
