#!/usr/bin/env bash
# test_check_no_unguarded_s3_delete.sh — Tests for check-no-unguarded-s3-delete.sh (#4991)
#
# Usage:
#   scripts/tests/test_check_no_unguarded_s3_delete.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-unguarded-s3-delete.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    mkdir -p "$(dirname "$TMPDIR_TEST/$name")"
    printf '%s\n' "$content" > "$TMPDIR_TEST/$name"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "${TMPDIR_TEST:?}"/*
}

create_test_file "cleanup.py" 's3.delete_objects(Bucket=b, Delete={"Objects": objs})'
assert_fails "boto3 delete_objects is detected"
reset_tmpdir

create_test_file "cleanup2.py" 'client.delete_object(Bucket=b, Key=k)'
assert_fails "boto3 delete_object is detected"
reset_tmpdir

create_test_file "wipe.sh" 'aws s3 rm "s3://bucket/ca/x/y/raw/abc.pdf"'
assert_fails "aws s3 rm is detected"
reset_tmpdir

create_test_file "wipe2.sh" 'aws s3api delete-object --bucket b --key k'
assert_fails "aws s3api delete-object is detected"
reset_tmpdir

create_test_file "prune.ts" 'await s3.send(new DeleteObjectCommand({ Bucket, Key }));'
assert_fails "JS SDK DeleteObjectCommand is detected"
reset_tmpdir

create_test_file "tidy.py" '# s3-delete-not-raw: removes only tmp/ uploads
s3.delete_object(Bucket=b, Key="tmp/upload.txt")'
assert_passes "a file with a s3-delete-not-raw marker passes"
reset_tmpdir

create_test_file "empty_marker.py" '# s3-delete-not-raw:
s3.delete_object(Bucket=b, Key=k)'
assert_fails "a marker with no reason does not count"
reset_tmpdir

create_test_file "notes.py" '# never call s3.delete_objects( on raws directly'
assert_passes "a comment line naming the call passes"
reset_tmpdir

create_test_file "tests/test_cleanup.py" 's3.delete_objects(Bucket=b, Delete={})'
assert_passes "files under tests/ are not scanned"
reset_tmpdir

create_test_file "packages/scraper-framework/src/framework/raw_delete_guard.py" \
    's3.delete_object(Bucket=bucket, Key=key)'
assert_passes "the guard module itself may delete"
reset_tmpdir

create_test_file "audit.py" 'from framework.raw_delete_guard import remove_delete_marker
remove_delete_marker(s3, bucket, key, marker_version_id)'
assert_passes "a caller of the guard module passes"
reset_tmpdir

# The real repository must be clean.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: the repository has no unguarded S3 delete"
else
    echo "FAIL: the repository has an unguarded S3 delete"
    FAILURES=$((FAILURES + 1))
fi

# The failure output carries a Fix block.
create_test_file "cleanup.py" 's3.delete_objects(Bucket=b, Delete={})'
TESTS=$((TESTS + 1))
out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if grep -q '^  Fix:' <<< "$out" && grep -q 'delete_raw_with_live_copy' <<< "$out"; then
    echo "PASS: failure output has a Fix block naming the guard"
else
    echo "FAIL: failure output lacks the Fix block"
    FAILURES=$((FAILURES + 1))
fi

echo ""
echo "$((TESTS - FAILURES))/$TESTS passed"
[[ $FAILURES -eq 0 ]]
