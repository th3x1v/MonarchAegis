#!/usr/bin/env bash
# Build the test image and run the pytest suite in-container.
#
#   ./run_tests.sh                       # full suite
#   ./run_tests.sh -m memory --memray    # memory tests with memray enforcement
#   ./run_tests.sh -k targets            # any extra args are passed to pytest
#
# Tests run in-container because local Python lacks the runtime deps and memray
# is Linux-only. cwd is /app so the app's relative static/ mount resolves.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# cygpath on Git-Bash/Windows; identity elsewhere.
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }

docker build -f "$root/Dockerfile.test" -t monarchaegis-test "$root" >/dev/null
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(winpath "$root/app")":/app \
  -v "$(winpath "$root/tests")":/tests \
  -w /app monarchaegis-test pytest /tests "$@"
