# Judgemind API container image.
#
# Multi-stage build: compile TypeScript, then run with production deps only.
#
# Build context must be the repo root:
#   docker build -t judgemind/api -f packages/api/Dockerfile .
# Run:
#   docker run -p 3001:3001 -e DATABASE_URL=... judgemind/api

FROM node:20-slim AS builder

WORKDIR /app
COPY packages/api/package.json packages/api/package-lock.json ./
RUN npm ci
COPY packages/api/tsconfig.json ./
COPY packages/api/src/ ./src/
RUN npm run build

FROM node:20-slim AS runtime

WORKDIR /app

# Use the built-in 'node' user (uid/gid 1000) from the base image
# instead of creating a new user, which fails when GID 1000 already exists.

COPY packages/api/package.json packages/api/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=builder /app/dist/ ./dist/
COPY packages/api/migrations/ ./migrations/
COPY packages/api/scripts/ ./scripts/

# Embed data-quality baselines for per-county threshold overrides in the
# dataQualityOverview resolver.  Build context is repo root (mirrors
# packages/scraper-framework/Dockerfile:66).  See #2981.
COPY data-quality-baselines.json /app/data-quality-baselines.json

RUN chown -R node:node /app
USER node

EXPOSE 3001

CMD ["node", "dist/index.js"]
