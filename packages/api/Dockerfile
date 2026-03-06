# Judgemind API container image.
#
# Multi-stage build: compile TypeScript, then run with production deps only.
#
# Build:  docker build -t judgemind/api .
# Run:    docker run -p 3001:3001 -e DATABASE_URL=... judgemind/api

FROM node:20-slim AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

FROM node:20-slim AS runtime

WORKDIR /app

# Non-root user for runtime security
RUN groupadd --gid 1000 api \
    && useradd --uid 1000 --gid api --create-home api

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=builder /app/dist/ ./dist/
COPY migrations/ ./migrations/

RUN chown -R api:api /app
USER api

EXPOSE 3001

CMD ["node", "dist/index.js"]
