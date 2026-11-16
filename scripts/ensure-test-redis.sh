#!/usr/bin/env bash
# ensure-test-redis.sh — the local Redis for the real-Redis tests.
#
# permanent: true
#
# Prints a JUDGEMIND_TEST_REDIS_URL on stdout for the local docker-compose
# Redis when it answers PING. All messages go to stderr. The pre-push hook
# calls it so the real-Redis tests (packages/*/tests that read
# JUDGEMIND_TEST_REDIS_URL, e.g. scraper-framework's TestRealRedis lease
# tests) run without the operator exporting anything (#5106). It is the
# Redis counterpart of scripts/ensure-test-db.sh.
#
# The URL selects database 15 so test keys stay out of database 0, which
# local dev processes use. Tests use unique key names and delete what they
# create, so nothing is flushed.
#
# Usage:
#   scripts/ensure-test-redis.sh
#
# Environment:
#   ENSURE_TEST_REDIS_HOST  default localhost (only local hosts accepted)
#   ENSURE_TEST_REDIS_PORT  default 6379 (the docker-compose service)
#   ENSURE_TEST_REDIS_DB    default 15
#
# Exit codes:
#   0  URL printed
#   1  error: a non-local host. A FAILED message and Fix: block are on
#      stderr.
#   2  usage error
#   3  Redis unavailable (nothing answers PING). A WARNING and Fix: block
#      are on stderr; callers degrade (the tests skip), they do not fail.

set -uo pipefail

HOST="${ENSURE_TEST_REDIS_HOST:-localhost}"
PORT="${ENSURE_TEST_REDIS_PORT:-6379}"
DB="${ENSURE_TEST_REDIS_DB:-15}"

case "${1:-}" in
    "") ;;
    -h|--help)
        sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ensure-test-redis.sh: unknown argument: $1" >&2
        exit 2
        ;;
esac

case "$HOST" in
    localhost|127.0.0.1|::1) ;;
    *)
        echo "FAILED: test Redis host '$HOST' is not local; refusing to write test keys there." >&2
        echo "Fix:" >&2
        echo "  unset ENSURE_TEST_REDIS_HOST     # use the docker-compose Redis on localhost" >&2
        exit 1
        ;;
esac

case "$PORT" in
    ''|*[!0-9]*)
        echo "ensure-test-redis.sh: ENSURE_TEST_REDIS_PORT must be a number; got '$PORT'" >&2
        exit 2
        ;;
esac

unavailable() {
    echo "WARNING: no local Redis for the real-Redis tests: $1" >&2
    echo "Fix:" >&2
    echo "  docker compose up -d redis     # from the repo root; then re-run" >&2
    exit 3
}

# PING over bash's /dev/tcp: no redis-cli needed on the host. The connect
# runs in a subshell so a refused connection cannot leave fd 3 open here.
reply="$(
    exec 2>/dev/null
    exec 3<>"/dev/tcp/$HOST/$PORT" || exit 1
    printf 'PING\r\n' >&3
    IFS= read -r -t 3 line <&3 || exit 1
    printf '%s' "${line%$'\r'}"
)" || unavailable "nothing answers on $HOST:$PORT"

if [ "$reply" != "+PONG" ]; then
    unavailable "$HOST:$PORT answered '$reply' to PING, not +PONG"
fi

echo "ensure-test-redis: using Redis at $HOST:$PORT (database $DB)" >&2
echo "redis://$HOST:$PORT/$DB"
