# _tf_archive_lifecycle_expiry.awk — find Terraform lifecycle rules that
# expire objects in the raw archive outside the throwaway prefixes (#5089).
#
# A lifecycle expiration deletes objects without a DeleteObject call, so the
# archive's bucket policy cannot stop it. Raws are irreplaceable
# (architecture-spec §3.3.3): an expiration may only filter on a throwaway
# prefix (spotcheck/, tmp/, migrations/).
#
# Used by scripts/check-no-unguarded-s3-delete.sh (every *.tf under infra/)
# and infra/terraform/modules/storage/tests/policy-checks/check.sh (check 4).
#
# Usage:
#   awk -v allowed="spotcheck/ tmp/ migrations/" \
#       -f scripts/_tf_archive_lifecycle_expiry.awk FILE.tf...
#
# For each `resource "aws_s3_bucket_lifecycle_configuration"` whose
# `bucket =` mentions "archive", walks every `rule {` and `dynamic "rule" {`
# block by brace depth (any indent). A rule that expires anything
# (`expiration {`, `noncurrent_version_expiration {`, or a `dynamic` block
# of either) must set at least one prefix, and every prefix must be a
# literal in the allowed list. A prefix built from an expression
# (`rule.value.prefix`, `var.x`) cannot be checked, so it fails. Prints one
# line per bad rule:
#
#   FILE:LINE:rule <id> expires archive objects outside the allowed prefixes (prefix: <values>)

function braces(s, ch,    t) {
    t = s
    return gsub(ch, "", t)
}

BEGIN {
    n = split(allowed, list, " ")
    for (i = 1; i <= n; i++) ok[list[i]] = 1
}

FNR == 1 { depth = 0; inlc = 0; inrule = 0 }

# HCL comment lines may name anything as context.
/^[ \t]*(#|\/\/)/ { next }

{
    line = $0
    before = depth
    if (!inlc && line ~ /resource[ \t]+"aws_s3_bucket_lifecycle_configuration"/) {
        inlc = 1; lc_depth = before; bucket = ""; nbad = 0
    }
    if (inlc && !inrule && before == lc_depth + 1 && line ~ /^[ \t]*bucket[ \t]*=/) {
        bucket = line
    }
    if (inlc && !inrule && before == lc_depth + 1 \
        && line ~ /^[ \t]*(rule|dynamic[ \t]+"rule")[ \t]*\{/) {
        inrule = 1; rule_depth = before; rule_line = FNR
        id = "(no id)"; prefixes = 0; prefix_bad = 0; prefix_txt = ""; expires = 0
    }
    if (inrule) {
        if (line ~ /^[ \t]*id[ \t]*=/) {
            id = line; sub(/^[^=]*=[ \t]*/, "", id); sub(/[ \t]*$/, "", id)
        }
        if (line ~ /(^|[^A-Za-z0-9_])prefix[ \t]*=/) {
            p = line; sub(/^.*prefix[ \t]*=[ \t]*/, "", p)
            sub(/[ \t}]*$/, "", p)
            prefixes++
            prefix_txt = prefix_txt (prefix_txt == "" ? "" : ", ") p
            if (p ~ /^"[^"$]*"$/) {
                q = substr(p, 2, length(p) - 2)
                if (!(q in ok)) prefix_bad = 1
            } else {
                prefix_bad = 1
            }
        }
        if (line ~ /(^|[^A-Za-z0-9_])(noncurrent_version_)?expiration[ \t]*\{/ \
            || line ~ /dynamic[ \t]+"(noncurrent_version_)?expiration"/) {
            expires = 1
        }
    }
    depth = before + braces(line, "[{]") - braces(line, "[}]")
    if (inrule && depth <= rule_depth) {
        inrule = 0
        if (expires && (prefixes == 0 || prefix_bad)) {
            bad[++nbad] = FILENAME ":" rule_line ":rule " id \
                " expires archive objects outside the allowed prefixes (prefix: " \
                (prefix_txt == "" ? "none, the whole bucket" : prefix_txt) ")"
        }
    }
    if (inlc && depth <= lc_depth) {
        inlc = 0
        if (tolower(bucket) ~ /archive/) {
            for (i = 1; i <= nbad; i++) print bad[i]
        }
    }
}
