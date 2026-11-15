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
    # find, not a glob: fixtures under .github/ are dot-directories.
    find "${TMPDIR_TEST:?}" -mindepth 1 -delete
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

# ── #5013: forms the original pattern missed ───────────────────────────────

create_test_file "move.sh" 'aws s3 mv "s3://bucket/ca/x/y/raw/a.pdf" "s3://bucket/old/a.pdf"'
assert_fails "aws s3 mv (copy then delete) is detected"
reset_tmpdir

create_test_file "drop.sh" 'aws s3 rb s3://bucket --force'
assert_fails "aws s3 rb is detected"
reset_tmpdir

create_test_file "mirror.sh" 'aws s3 sync ./local "s3://bucket/ca/" --delete --quiet'
assert_fails "aws s3 sync --delete is detected"
reset_tmpdir

create_test_file "mirror_ok.sh" 'aws s3 sync "s3://bucket/ca/" ./local --quiet'
assert_passes "aws s3 sync without --delete passes"
reset_tmpdir

create_test_file "res1.py" 'boto3.resource("s3").Object(bucket, key).delete()'
assert_fails "boto3 resource Object(...).delete() is detected"
reset_tmpdir

create_test_file "res2.py" 'bucket.objects.filter(Prefix="ca/").delete()'
assert_fails "boto3 resource objects.filter(...).delete() is detected"
reset_tmpdir

create_test_file "res3.py" 'bucket.objects.all().delete()'
assert_fails "boto3 resource objects.all().delete() is detected"
reset_tmpdir

create_test_file "res4.py" 'bucket.object_versions.delete()'
assert_fails "boto3 resource object_versions.delete() is detected"
reset_tmpdir

create_test_file "res5.py" 's3 = session.resource("s3")
obj = s3.Object(bucket, key)
obj.delete()'
assert_fails "a .delete() in a file that builds an S3 resource is detected"
reset_tmpdir

create_test_file "orm.py" 'session.delete(row)
cache.delete("k")'
assert_passes "an unrelated .delete( call in a file without an S3 resource passes"
reset_tmpdir

create_test_file "lifecycle.py" 's3.put_bucket_lifecycle_configuration(Bucket=b, LifecycleConfiguration=c)'
assert_fails "a boto3 lifecycle write is detected"
reset_tmpdir

create_test_file "lifecycle.sh" 'aws s3api put-bucket-lifecycle-configuration --bucket b --lifecycle-configuration file://c.json'
assert_fails "an AWS CLI lifecycle write is detected"
reset_tmpdir

create_test_file ".github/workflows/cleanup.yml" '      - run: aws s3 rm "s3://bucket/ca/" --recursive'
assert_fails "a delete in a .github/ workflow is detected"
reset_tmpdir

create_test_file "infra/terraform/modules/x/main.tf" '    command = "aws s3 mv s3://b/ca/a s3://b/tmp/a"'
assert_fails "a delete in a terraform local-exec is detected"
reset_tmpdir

# Default scan roots include .github/ and infra/ (and not docs/).
FIXTURE_ROOT="$TMPDIR_TEST/root"
mkdir -p "$FIXTURE_ROOT/.github/workflows" "$FIXTURE_ROOT/infra" "$FIXTURE_ROOT/docs"
printf '%s\n' '      - run: aws s3 rm s3://bucket/ca/ --recursive' > "$FIXTURE_ROOT/.github/workflows/w.yml"
TESTS=$((TESTS + 1))
if S3_DELETE_CHECK_REPO_ROOT="$FIXTURE_ROOT" "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "FAIL: default scan roots include .github/ (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: default scan roots include .github/"
fi
rm -f "$FIXTURE_ROOT/.github/workflows/w.yml"
printf '%s\n' 'command = "aws s3 rm s3://bucket/ca/x"' > "$FIXTURE_ROOT/infra/main.tf"
TESTS=$((TESTS + 1))
if S3_DELETE_CHECK_REPO_ROOT="$FIXTURE_ROOT" "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "FAIL: default scan roots include infra/ (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: default scan roots include infra/"
fi
rm -f "$FIXTURE_ROOT/infra/main.tf"
printf '%s\n' 'aws s3 rm s3://bucket/ca/x' > "$FIXTURE_ROOT/docs/notes.sh"
TESTS=$((TESTS + 1))
if S3_DELETE_CHECK_REPO_ROOT="$FIXTURE_ROOT" "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: docs/ is not a default scan root"
else
    echo "FAIL: docs/ is not a default scan root (expected success, got failure)"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ── #5013: the marker covers the call below it, not the whole file ─────────

create_test_file "two_calls.py" '# s3-delete-not-raw: removes only tmp/ uploads
s3.delete_object(Bucket=b, Key="tmp/upload.txt")


def other():
    x = 1
    s3.delete_object(Bucket=b, Key=raw_key)'
assert_fails "a marker does not cover a second, unmarked call in the same file"
reset_tmpdir

create_test_file "far_marker.py" '# s3-delete-not-raw: removes only tmp/ uploads
a = 1
b = 2
c = 3
d = 4
s3.delete_object(Bucket=b, Key=k)'
assert_fails "a marker more than 3 lines above the call does not count"
reset_tmpdir

create_test_file "same_line.sh" 'aws s3 rm "s3://assets/oneshot-scripts/x.py"  # s3-delete-not-raw: staged oneshot script'
assert_passes "a marker on the same line passes"
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
