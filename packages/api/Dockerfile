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

# Use the built-in 'node' user (uid/gid 1000) from the base image
# instead of creating a new user, which fails when GID 1000 already exists.

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=builder /app/dist/ ./dist/
COPY migrations/ ./migrations/

RUN chown -R node:node /app
USER node

EXPOSE 3001

CMD ["node", "dist/index.js"]
