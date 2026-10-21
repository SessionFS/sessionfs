#!/bin/sh
# Works-in harness job entrypoint. The image runs the harness as root on
# purpose: `sfs catalog harness run` drops every tool command to the
# unprivileged sfs-tool user and needs root to reap that user's processes.
# Opt-in (SFS_WORKS_IN_HARNESS=1), the API URL and the skills_verify:run key
# come from the job's environment, never from the image.
set -eu
exec sfs catalog harness run --tool "${SFS_HARNESS_TOOL:?SFS_HARNESS_TOOL is baked into the image}" "$@"
