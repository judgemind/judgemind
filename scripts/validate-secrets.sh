#!/usr/bin/env bash
# validate-secrets.sh — Validate deployed secrets in AWS Secrets Manager.
#
# Checks that secret values are well-formed and that the referenced hosts are
# reachable (TCP SYN/ACK only — no authentication). This catches classes of
# bugs like invalid sslmode values, typos in hostnames, or wrong ports before
# they crash-loop ECS tasks.
#
# Usage:
#   scripts/validate-secrets.sh [--env dev|staging|production] [--skip-connectivity]
#
# Options:
#   --env ENV             Environment to validate (default: dev)
#   --skip-connectivity   Only validate secret format, skip TCP connectivity checks
#
# Requires:
#   - AWS CLI v2 with credentials for the judgemind account
#   - python3 (for JSON parsing)
#
# Exit codes:
#   0  All checks passed
#   1  One or more checks failed

set -euo pipefail

# ─── Defaults ──────────────────────────────────────────────────────────────────

ENVIRONMENT="dev"
SKIP_CONNECTIVITY=false
REGION="us-west-2"
ERRORS=0

# ─── Parse arguments ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --skip-connectivity)
            SKIP_CONNECTIVITY=true
            shift
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            echo "Usage: scripts/validate-secrets.sh [--env dev|staging|production] [--skip-connectivity]" >&2
            exit 1
            ;;
    esac
done

# Validate environment
case "$ENVIRONMENT" in
    dev|staging|production) ;;
    *)
        echo "Error: invalid environment '$ENVIRONMENT'. Must be dev, staging, or production." >&2
        exit 1
        ;;
esac

# ─── Helpers ───────────────────────────────────────────────────────────────────

pass() {
    echo "  PASS: $1"
}

fail() {
    echo "  FAIL: $1" >&2
    ERRORS=$((ERRORS + 1))
}

info() {
    echo "  INFO: $1"
}

# Check TCP connectivity to host:port with a 5-second timeout.
# Uses bash /dev/tcp or python3 as fallback.
check_tcp() {
    local host="$1"
    local port="$2"
    local label="$3"

    if "$SKIP_CONNECTIVITY"; then
        info "Skipping connectivity check for $label ($host:$port)"
        return
    fi

    # Use python3 for portable TCP check (bash /dev/tcp is not available everywhere)
    if python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(('$host', $port))
    s.close()
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
        pass "$label is reachable at $host:$port"
    else
        fail "$label is not reachable at $host:$port"
    fi
}

# ─── Validate DATABASE_URL ─────────────────────────────────────────────────────

echo ""
echo "=== Validating secrets for environment: $ENVIRONMENT ==="
echo ""

DB_SECRET_ID="judgemind/$ENVIRONMENT/db/connection"
echo "--- $DB_SECRET_ID ---"

db_secret=$(aws secretsmanager get-secret-value \
    --secret-id "$DB_SECRET_ID" \
    --query SecretString \
    --output text \
    --region "$REGION" 2>/dev/null) || true

if [[ -z "$db_secret" ]]; then
    fail "Could not retrieve secret '$DB_SECRET_ID'"
else
    pass "Secret '$DB_SECRET_ID' exists"

    # Extract the URL field
    db_url=$(echo "$db_secret" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('url', ''), end='')
" 2>/dev/null) || true

    if [[ -z "$db_url" ]]; then
        fail "Secret '$DB_SECRET_ID' has no 'url' field"
    else
        pass "Secret has 'url' field"

        # Validate sslmode
        VALID_SSLMODES="disable allow prefer require verify-ca verify-full"

        # Extract sslmode from the URL query string
        sslmode=$(echo "$db_url" | python3 -c "
import sys
from urllib.parse import urlparse, parse_qs
url = sys.stdin.read().strip()
parsed = urlparse(url)
qs = parse_qs(parsed.query)
modes = qs.get('sslmode', [])
print(modes[0] if modes else '', end='')
" 2>/dev/null) || true

        if [[ -z "$sslmode" ]]; then
            info "No sslmode in DATABASE_URL (PostgreSQL defaults to 'prefer')"
        elif echo "$VALID_SSLMODES" | grep -qw "$sslmode"; then
            pass "sslmode='$sslmode' is valid"
        else
            fail "sslmode='$sslmode' is not a valid PostgreSQL sslmode. Valid values: $VALID_SSLMODES"
        fi

        # Validate URL scheme
        scheme=$(echo "$db_url" | python3 -c "
import sys
from urllib.parse import urlparse
print(urlparse(sys.stdin.read().strip()).scheme, end='')
" 2>/dev/null) || true

        case "$scheme" in
            postgresql|postgres)
                pass "URL scheme '$scheme' is valid"
                ;;
            *)
                fail "URL scheme '$scheme' is not valid (expected 'postgresql' or 'postgres')"
                ;;
        esac

        # Extract host and port for connectivity check
        db_host=$(echo "$db_url" | python3 -c "
import sys
from urllib.parse import urlparse
print(urlparse(sys.stdin.read().strip()).hostname or '', end='')
" 2>/dev/null) || true

        db_port=$(echo "$db_url" | python3 -c "
import sys
from urllib.parse import urlparse
print(urlparse(sys.stdin.read().strip()).port or 5432, end='')
" 2>/dev/null) || true

        if [[ -n "$db_host" ]]; then
            check_tcp "$db_host" "$db_port" "PostgreSQL"
        else
            fail "Could not extract host from DATABASE_URL"
        fi
    fi

    # Validate individual fields exist
    for field in host port dbname username password; do
        has_field=$(echo "$db_secret" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('yes' if '$field' in data and data['$field'] else 'no', end='')
" 2>/dev/null) || true

        if [[ "$has_field" == "yes" ]]; then
            pass "Secret has '$field' field"
        else
            fail "Secret is missing '$field' field"
        fi
    done
fi

# ─── Validate REDIS_URL ───────────────────────────────────────────────────────

echo ""
echo "--- Redis URL (from Terraform outputs) ---"

# Redis URL is not in Secrets Manager — it's a plain environment variable
# derived from the ElastiCache endpoint. We check it via Terraform output
# or the ECS task definition.
# For now, attempt to resolve the ElastiCache endpoint from the compute module.

redis_endpoint=$(aws elasticache describe-cache-clusters \
    --region "$REGION" \
    --query "CacheClusters[?starts_with(CacheClusterId,'judgemind-$ENVIRONMENT')].CacheNodes[0].Endpoint.[Address,Port]" \
    --output text 2>/dev/null) || true

if [[ -z "$redis_endpoint" || "$redis_endpoint" == "None" ]]; then
    info "No ElastiCache cluster found for environment '$ENVIRONMENT' (may not be provisioned yet)"
else
    redis_host=$(echo "$redis_endpoint" | awk '{print $1}')
    redis_port=$(echo "$redis_endpoint" | awk '{print $2}')

    if [[ -n "$redis_host" && "$redis_host" != "None" ]]; then
        pass "Redis endpoint resolved: $redis_host:$redis_port"
        check_tcp "$redis_host" "$redis_port" "Redis"
    else
        info "Could not parse Redis endpoint"
    fi
fi

# ─── Validate OPENSEARCH_URL ──────────────────────────────────────────────────

echo ""
echo "--- OpenSearch ---"

os_secret_id="judgemind/$ENVIRONMENT/opensearch/credentials"
os_secret=$(aws secretsmanager get-secret-value \
    --secret-id "$os_secret_id" \
    --query SecretString \
    --output text \
    --region "$REGION" 2>/dev/null) || true

if [[ -z "$os_secret" ]]; then
    # OpenSearch credentials might be under a different naming convention;
    # check the domain endpoint directly.
    info "No OpenSearch credentials secret found at '$os_secret_id'"
else
    pass "Secret '$os_secret_id' exists"

    for field in username password; do
        has_field=$(echo "$os_secret" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('yes' if '$field' in data and data['$field'] else 'no', end='')
" 2>/dev/null) || true

        if [[ "$has_field" == "yes" ]]; then
            pass "OpenSearch secret has '$field' field"
        else
            fail "OpenSearch secret is missing '$field' field"
        fi
    done
fi

# Check OpenSearch domain endpoint reachability
os_endpoint=$(aws opensearch describe-domain \
    --domain-name "judgemind-$ENVIRONMENT" \
    --region "$REGION" \
    --query "DomainStatus.Endpoints.vpc" \
    --output text 2>/dev/null) || true

if [[ -z "$os_endpoint" || "$os_endpoint" == "None" ]]; then
    info "No OpenSearch domain found for environment '$ENVIRONMENT' (may not be provisioned yet)"
else
    pass "OpenSearch endpoint resolved: $os_endpoint"
    check_tcp "$os_endpoint" 443 "OpenSearch"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "=== Summary ==="
if [[ "$ERRORS" -eq 0 ]]; then
    echo "All checks passed."
    exit 0
else
    echo "$ERRORS check(s) failed." >&2
    exit 1
fi
