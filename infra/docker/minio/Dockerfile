# MinIO server + mc client, built from pinned source (#4660).
#
# Why build from source instead of pulling an image:
#   - Docker Hub's minio/minio and minio/mc were removed (#4658).
#   - quay.io/minio/{minio,mc} stopped allowing anonymous pulls (#4660).
#   - dl.min.io binary downloads return 410 Gone.
#   - github.com/minio/minio and github.com/minio/mc are archived upstream,
#     so no new releases are coming anyway.
#
# The Go module proxy (proxy.golang.org) caches module versions immutably
# and `go install` verifies them against the checksum DB (sum.golang.org),
# so pinning to the pseudo-versions below gives a reproducible, tamper-evident
# build that no longer depends on MinIO's registry policy. Base images are
# Docker Official Images pinned by index digest.
#
# Pinned versions (tag -> pseudo-version):
#   minio RELEASE.2025-10-15T17-29-55Z -> v0.0.0-20251015172955-9e49d5e7a648
#   mc    RELEASE.2025-08-13T08-35-41Z -> v0.0.0-20250813083541-7394ce0dd2a8
#
# Used by:
#   - .github/ecs-oneshot-test/run-tests.sh (S3 presigned-URL delivery test,
#     including the IAM policy that mirrors production)
#   - docker-compose.yml `minio` service (local dev)
#
# The image entrypoint is `minio`; run `mc` with `--entrypoint mc` (or `sh -c`).

# golang:1.24-bookworm
FROM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS build

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local \
    GOFLAGS=-trimpath

# Versions are inlined (not ARGs) so the module version and the stamped
# release tag cannot drift apart. Stamp the upstream release tag the same way MinIO's own
# buildscripts/gen-ldflags.go does, so `minio --version` / `mc --version`
# report the release rather than DEVELOPMENT.GOGET.
RUN go install -ldflags "-s -w \
        -X github.com/minio/minio/cmd.Version=2025-10-15T17:29:55Z \
        -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2025-10-15T17-29-55Z \
        -X github.com/minio/minio/cmd.CommitID=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
        -X github.com/minio/minio/cmd.ShortCommitID=9e49d5e7a648" \
    github.com/minio/minio@v0.0.0-20251015172955-9e49d5e7a648
RUN go install -ldflags "-s -w \
        -X github.com/minio/mc/cmd.Version=2025-08-13T08:35:41Z \
        -X github.com/minio/mc/cmd.ReleaseTag=RELEASE.2025-08-13T08-35-41Z \
        -X github.com/minio/mc/cmd.CommitID=7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
        -X github.com/minio/mc/cmd.ShortCommitID=7394ce0dd2a8" \
    github.com/minio/mc@v0.0.0-20250813083541-7394ce0dd2a8

# debian:bookworm-slim
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

COPY --from=build /go/bin/minio /usr/local/bin/minio
COPY --from=build /go/bin/mc /usr/local/bin/mc

EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
