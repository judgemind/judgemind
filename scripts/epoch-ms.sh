#!/usr/bin/env bash
# Output the current epoch time in milliseconds, with an optional offset
# in seconds.
#
# Usage:
#   scripts/epoch-ms.sh          # current time in epoch milliseconds
#   scripts/epoch-ms.sh -600     # 10 minutes ago
#   scripts/epoch-ms.sh 3600     # 1 hour from now
#
# The offset is added to the current time (use negative values for the past).

set -euo pipefail

offset=${1:-0}

python3 -c "import time; print(int((time.time() + ${offset}) * 1000))"
