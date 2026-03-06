#!/usr/bin/env bash
# with-secret.sh — Fetch secrets from AWS Secrets Manager and run a command
# with them as environment variables. Secrets never touch disk or stdout.
#
# Usage:
#   scripts/with-secret.sh -e VAR=secret-id:.jq_field [-e ...] -- command [args...]
#
# Examples:
#   scripts/with-secret.sh -e CF_API_TOKEN=judgemind/cloudflare:.token -- terraform apply
#   scripts/with-secret.sh -e DB_PASS=judgemind/rds:.password -e API_KEY=judgemind/api:.key -- ./run.sh
#   scripts/with-secret.sh -e RAW_SECRET=judgemind/simple -- echo "secret is set"
#
# The :.field suffix extracts a JSON key from the secret string using python3.
# Omit the :.field suffix to use the raw SecretString value.

set -euo pipefail

declare -a ENV_SPECS=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -e)
            if [[ $# -lt 2 ]]; then
                echo "Error: -e requires an argument (VAR=secret-id[:. field])" >&2
                exit 1
            fi
            ENV_SPECS+=("$2")
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "Error: unexpected argument '$1'. Use -e VAR=secret-id -- command" >&2
            exit 1
            ;;
    esac
done

if [[ ${#ENV_SPECS[@]} -eq 0 ]]; then
    echo "Error: at least one -e VAR=secret-id is required" >&2
    exit 1
fi

if [[ $# -eq 0 ]]; then
    echo "Error: no command specified after --" >&2
    exit 1
fi

# Resolve each secret and export it
for spec in "${ENV_SPECS[@]}"; do
    # Split on first =
    var_name="${spec%%=*}"
    secret_spec="${spec#*=}"

    if [[ -z "$var_name" || "$var_name" == "$spec" ]]; then
        echo "Error: invalid spec '$spec'. Format: VAR=secret-id[:.field]" >&2
        exit 1
    fi

    # Split secret_spec on :. to get secret_id and optional jq field
    if [[ "$secret_spec" == *:.* ]]; then
        secret_id="${secret_spec%%:.*}"
        json_field="${secret_spec#*.}"
    else
        secret_id="$secret_spec"
        json_field=""
    fi

    # Fetch the secret from AWS Secrets Manager
    raw_secret=$(aws secretsmanager get-secret-value \
        --secret-id "$secret_id" \
        --query SecretString \
        --output text \
        --region us-west-2 2>/dev/null)

    if [[ $? -ne 0 || -z "$raw_secret" ]]; then
        echo "Error: failed to retrieve secret '$secret_id'" >&2
        exit 1
    fi

    # Extract field if specified, otherwise use raw value
    if [[ -n "$json_field" ]]; then
        value=$(echo "$raw_secret" | python3 -c "
import sys, json
data = json.load(sys.stdin)
key = '$json_field'
if key not in data:
    print(f'Error: key \"{key}\" not found in secret', file=sys.stderr)
    sys.exit(1)
print(data[key], end='')
" 2>/dev/null)

        if [[ $? -ne 0 ]]; then
            echo "Error: failed to extract field '$json_field' from secret '$secret_id'" >&2
            exit 1
        fi
    else
        value="$raw_secret"
    fi

    export "$var_name=$value"
done

# Execute the command with the secrets in the environment
exec "$@"
