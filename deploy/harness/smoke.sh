#!/usr/bin/env bash
# Smoke-test a built works-in harness image (no network, no credentials).
#
#   deploy/harness/smoke.sh <image-ref> <tool>
#
# 1. `sfs --help` works;
# 2. the tool CLI's version command works as the unprivileged sfs-tool user;
# 3. the harness self-check passes (tool user resolution, the harness runner
#    dropping to that uid, the post-job reaper + /proc verification, and the
#    pinned version);
# 4. the tool's own installer accepts a fixture package through the real
#    harness job path (fixture_check.py), offline;
# 5. the entrypoint fails closed: without SFS_WORKS_IN_HARNESS=1 it exits 2,
#    and with it but without SFS_HARNESS_API_URL/KEY it also exits 2.
set -euo pipefail

image="${1:?image ref}"
tool="${2:?tool}"
here="$(cd "$(dirname "$0")" && pwd)"
binary="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tools"][sys.argv[2]]["binary"])' \
  "$here/tools.json" "$tool")"
run() { docker run --rm --network none "$@"; }

echo "== sfs --help"
run --entrypoint sfs "$image" --help >/dev/null

echo "== $binary --version (as sfs-tool)"
run --user sfs-tool -e HOME=/tmp --entrypoint "$binary" "$image" --version

echo "== harness self-check"
run --entrypoint python "$image" -I /opt/sfs-harness/selfcheck.py

echo "== offline L1 install of a fixture package (real harness job path)"
run -v "$here/fixture_check.py:/fixture_check.py:ro" --entrypoint python "$image" -I /fixture_check.py

expect_exit() {
  local want="$1"; shift
  set +e
  run "$@" >/dev/null 2>&1
  local got=$?
  set -e
  if [ "$got" != "$want" ]; then
    echo "expected exit $want, got $got: docker run $*" >&2
    exit 1
  fi
}
echo "== entrypoint refuses without opt-in"
expect_exit 2 "$image"
echo "== entrypoint refuses without a harness key"
expect_exit 2 -e SFS_WORKS_IN_HARNESS=1 "$image"
echo "smoke OK: $image ($tool)"
