# Vendored: judgemind-transcripts

This directory vendors the `render-transcript.py` script from the private repo
[`judgemind/judgemind-transcripts`](https://github.com/judgemind/judgemind-transcripts).

## Why vendor

The dispatcher v3 image (`Dockerfile.dispatcher-v3`, see issue #3886) needs
`render-transcript.py` baked at `/opt/judgemind-transcripts/render-transcript.py`
so the v3 task-runner entrypoint can produce the compact transcript that the
diagnoser reads on EXIT (spec §4.1 "Session capture: CloudWatch primary (raw),
S3 archive (compact)").

The upstream repo is private. Cloning at Docker build time would require:

- A build-time `GITHUB_TOKEN` build-arg that risks ending up in the image's
  layer history — even with a follow-up `rm` step, the token is recoverable
  from intermediate layers unless the build uses BuildKit secrets (which
  `deploy-dispatcher-v3.yml` does not).
- An SSH deploy key configured in the CI workflow — adds a secret-management
  surface that we don't otherwise need for a single 303-line script.

The task scope (#3886) explicitly authorized "COPY a vendored copy if
cloning is fragile." Vendoring sidesteps both problems: the file ships with
the bootstrap repo's source tree and is COPY'd at the canonical
`/opt/judgemind-transcripts/` path inside the image.

## What's vendored

- `render-transcript.py` — the JSONL → human-readable text renderer the
  v3 task-runner invokes on session EXIT.

The script itself is not sensitive (the upstream repo is private to keep
the *transcripts* private, not the renderer). A clean-room reader of this
file learns nothing they couldn't infer from the
`docs/specs/dispatcher-v3-spec.md` §4.1 description of the compact
transcript.

## Updating the vendored copy

When upstream `render-transcript.py` changes, refresh the vendor copy:

```bash
cp ~/judgemind/transcripts/render-transcript.py \
   vendor/judgemind-transcripts/render-transcript.py
```

Then commit the diff with a clear message that the bump comes from upstream.
The dispatcher-v3 image rebuilds automatically on changes under `vendor/`
(see `.github/workflows/deploy-dispatcher-v3.yml` paths filter).

## Why a top-level `vendor/` directory

Using `vendor/` (rather than nesting under `scripts/dispatcher_v3/` or
`packages/`) signals that this is a third-party copy that doesn't follow
the rest of the repo's conventions — no tests, no linting, no
ratcheted coverage. The upstream repo owns the code's correctness; we
just pin a known-good revision.
