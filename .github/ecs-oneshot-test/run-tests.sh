#!/usr/bin/env bash
# run-tests.sh — Run ECS oneshot integration tests in Docker.
#
# Builds a container image that simulates the ECS oneshot environment,
# then runs test scripts inside it using the same delivery mechanism
# as ecs-run-task.sh (base64 inline or direct copy).
#
# Usage:
#   .github/ecs-oneshot-test/run-tests.sh
#
# Exit code: 0 if all tests pass, 1 if any test fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="judgemind-ecs-oneshot-test"
TEST_SCRIPTS_DIR="$SCRIPT_DIR/test-scripts"

# Track pass/fail counts
PASS=0
FAIL=0
FAILURES=()

# AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461.
export AWS_PAGER=""

# ─── Build the test image ────────────────────────────────────────────────────

echo "=== Building ECS oneshot test image ==="
docker build \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t "$IMAGE_NAME" \
    "$REPO_ROOT"

echo "Image built successfully."
echo ""

# ─── Helper: run a script inside the container ──────────────────────────────
#
# Simulates the ecs-run-task.sh delivery mechanism:
#   1. Base64-encode the script, decode inside the container, and run it
#
# This matches the delivery logic in ecs-run-task.sh.

run_test() {
    local test_name="$1"
    local script_path="$2"
    shift 2
    local script_args=("$@")

    echo "--- Test: $test_name ---"

    # Base64-encode the script (same as ecs-run-task.sh for small scripts)
    local encoded
    # Strip newlines: Linux base64 wraps at 76 chars, which breaks bash -c
    encoded=$(base64 < "$script_path" | tr -d '\n')

    # Build the command string matching ecs-run-task.sh's pattern
    local args_str=""
    for arg in "${script_args[@]+"${script_args[@]}"}"; do
        local escaped
        escaped=$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")
        args_str="${args_str} '${escaped}'"
    done

    local cmd_str="echo ${encoded} | base64 -d > /tmp/_oneshot_script && python3 /tmp/_oneshot_script${args_str}"

    if docker run --rm "$IMAGE_NAME" "$cmd_str" 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL (exit code: $?)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$test_name")
    fi
    echo ""
}

# ─── MinIO sidecar helpers (S3 presigned URL delivery tests) ─────────────────
#
# setup_minio() launches a MinIO container that mirrors the production IAM
# model: an 'oneshot-uploader' user whose policy grants exactly the same
# permissions as AllowUploadSpotcheckOneshotScripts in dispatcher-agent-runner.
# teardown_minio() is registered as an EXIT trap so it always cleans up.

setup_minio() {
    echo "=== Setting up MinIO for S3 delivery tests ==="

    # Remove stale containers/network from a previous interrupted run
    docker rm -f minio 2>/dev/null || true
    docker network rm oneshot-test-net 2>/dev/null || true

    docker network create oneshot-test-net

    # Name the container 'minio' so other containers on oneshot-test-net
    # resolve it via docker DNS as 'minio:9000'.
    docker run -d \
        --name minio \
        --network oneshot-test-net \
        -p 9000:9000 \
        -e MINIO_ROOT_USER=minioadmin \
        -e MINIO_ROOT_PASSWORD=minioadmin \
        minio/minio:latest \
        server /data

    # Poll health endpoint (MinIO typically starts within 3s)
    local i=0
    while [[ $i -lt 30 ]]; do
        if curl -sf "http://localhost:9000/minio/health/ready" > /dev/null 2>&1; then
            echo "  MinIO ready after ${i}s."
            break
        fi
        sleep 1
        i=$((i + 1))
    done
    if [[ $i -ge 30 ]]; then
        echo "ERROR: MinIO did not become ready within 30s."
        return 1
    fi

    # Write IAM policy mirroring production AllowUploadSpotcheckOneshotScripts
    # (PutObject + GetObject + DeleteObject on oneshot-scripts/*)
    local policy_file
    policy_file="$(mktemp)"
    cat > "$policy_file" << 'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::oneshot-scripts-test/oneshot-scripts/*"]
    }
  ]
}
JSON

    # Configure MinIO: bucket + constrained user + policy attachment
    docker run --rm \
        --network oneshot-test-net \
        -v "${policy_file}:/tmp/uploader-policy.json" \
        --entrypoint sh \
        minio/mc:latest \
        -c "mc alias set local http://minio:9000 minioadmin minioadmin \
            && mc mb local/oneshot-scripts-test \
            && mc admin user add local oneshot-uploader 'upl-secret-key' \
            && mc admin policy create local uploader-policy /tmp/uploader-policy.json \
            && mc admin policy attach local uploader-policy --user oneshot-uploader"

    rm -f "$policy_file"
    echo "  MinIO configured: bucket=oneshot-scripts-test, user=oneshot-uploader"
    echo "  Policy: s3:PutObject+GetObject+DeleteObject on oneshot-scripts/*"
}

teardown_minio() {
    docker stop minio 2>/dev/null || true
    docker rm minio 2>/dev/null || true
    docker network rm oneshot-test-net 2>/dev/null || true
}

# ─── Helper: run a script via S3 presigned URL (large-script delivery path) ──
#
# Exercises ecs-run-task.sh:547-569: upload to S3 as the constrained IAM
# principal, mint a presigned URL, download inside a credential-less container.

run_test_s3_delivery() {
    echo "--- Test: S3 presigned URL delivery (>8KB script) ---"

    local script_path="$TEST_SCRIPTS_DIR/test_large_script.py"
    local s3_key="oneshot-scripts/test_large_script.py"

    # Upload using oneshot-uploader credentials (mirrors production IAM principal).
    # AWS_CONFIG_FILE forces path-style addressing so the bucket name is not
    # prepended as a subdomain of 'localhost' (which fails DNS resolution).
    local aws_config
    aws_config="$(mktemp)"
    printf '[default]\ns3 =\n  addressing_style = path\n' > "$aws_config"

    AWS_CONFIG_FILE="$aws_config" \
    AWS_ACCESS_KEY_ID="oneshot-uploader" \
    AWS_SECRET_ACCESS_KEY="upl-secret-key" \
    AWS_DEFAULT_REGION="us-east-1" \
    aws s3 cp "$script_path" "s3://oneshot-scripts-test/$s3_key" \
        --endpoint-url "http://localhost:9000"

    rm -f "$aws_config"

    # Generate presigned URL from inside the docker network so the URL contains
    # 'minio:9000' as the host (reachable by the credential-less test container)
    local presign_py
    presign_py="$(mktemp)"
    cat > "$presign_py" << 'EOF'
import boto3
from botocore.config import Config

s3 = boto3.client(
    's3',
    endpoint_url='http://minio:9000',
    region_name='us-east-1',
    aws_access_key_id='oneshot-uploader',
    aws_secret_access_key='upl-secret-key',
    config=Config(signature_version='s3v4', s3={'addressing_style': 'path'})
)
url = s3.generate_presigned_url(
    'get_object',
    Params={'Bucket': 'oneshot-scripts-test', 'Key': 'oneshot-scripts/test_large_script.py'},
    ExpiresIn=900
)
print(url)
EOF

    local presigned_url
    presigned_url=$(docker run --rm \
        --network oneshot-test-net \
        -v "${presign_py}:/tmp/presign.py" \
        "$IMAGE_NAME" \
        "python3 /tmp/presign.py")
    rm -f "$presign_py"

    # Reproduce the exact command string from ecs-run-task.sh:569 in production
    local cmd_str="python3 -c \"import urllib.request; urllib.request.urlretrieve('${presigned_url}', '/tmp/_oneshot_script')\" && python3 /tmp/_oneshot_script"

    # Run WITHOUT AWS credentials — only the presigned URL grants download access
    if docker run --rm \
        --network oneshot-test-net \
        "$IMAGE_NAME" \
        "$cmd_str" 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL (exit code: $?)"
        FAIL=$((FAIL + 1))
        FAILURES+=("S3 presigned URL delivery (>8KB)")
    fi
    echo ""
}

# ─── Run test scripts ────────────────────────────────────────────────────────

echo "=== Running ECS oneshot integration tests ==="
echo ""

run_test "import common dependencies" \
    "$TEST_SCRIPTS_DIR/test_imports.py"

run_test "framework internal imports" \
    "$TEST_SCRIPTS_DIR/test_framework_imports.py"

run_test "script argument passthrough" \
    "$TEST_SCRIPTS_DIR/test_script_args.py" \
    --dry-run --county "Los Angeles" --limit 10

# ─── S3 presigned URL delivery test ──────────────────────────────────────────
# Exercises the >8KB branch in ecs-run-task.sh:547 that uploads to S3 and
# delivers the script via a presigned URL instead of inline base64.

# Register teardown first so MinIO is always cleaned up on any exit
trap teardown_minio EXIT
setup_minio

run_test_s3_delivery

# ─── Summary ──────────────────────────────────────────────────────────────────

echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "Failed tests:"
    for name in "${FAILURES[@]}"; do
        echo "  - $name"
    done
    exit 1
fi

echo ""
echo "All tests passed."
