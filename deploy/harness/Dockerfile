# syntax=docker/dockerfile:1
#
# Works-in harness image (marketplace v2 §7.4, level L1): one image per tool.
#
#   docker build -f deploy/harness/Dockerfile --build-arg TOOL=claude-code \
#     -t sfs-harness-claude-code .          # context = repository root
#
# TOOL is a key of deploy/harness/tools.json "tools" (claude-code, copilot,
# gemini). The tool CLI is installed from the committed lockfile in
# deploy/harness/tools/<tool>/ at the exact version tools.json pins. Base
# images are pinned by tag AND digest (Dependabot's docker updater bumps
# both; the two python stages must stay identical, see
# tests/unit/test_harness_images.py).
#
# Runtime contract (src/sessionfs/skills/works_in_harness.py):
# - the harness runs as ROOT (no USER directive, deliberately): every tool
#   command is dropped to the unprivileged `sfs-tool` user, and root is
#   needed to reap that user's processes after the job;
# - nothing secret is baked in: SFS_WORKS_IN_HARNESS, SFS_HARNESS_API_URL,
#   SFS_HARNESS_API_KEY and SFS_HARNESS_IMAGE_DIGEST come from the job;
# - the tool sees only PATH from this image (the harness builds a minimal
#   environment per command), so update/telemetry switches are system-wide
#   config files (tools/<tool>/etc/), and the tool install under /opt is
#   root-owned so the tool user cannot self-update it.

# ---- tool CLI (npm ci from the pinned lockfile) ----
FROM node:26.11.1-bookworm-slim@sha256:86f07bc9c5dce4578cf37e5a418b7bfc7f817cda25cde66e2b66e95ed86c4567 AS tool
ARG TOOL
ENV TOOL=${TOOL} \
    NPM_CONFIG_UPDATE_NOTIFIER=false
COPY deploy/harness/tools.json /opt/sfs-harness/tools.json
COPY deploy/harness/install_tool.mjs /opt/sfs-harness/install_tool.mjs
COPY deploy/harness/tools/${TOOL}/ /opt/sfs-harness/tool/
RUN node /opt/sfs-harness/install_tool.mjs

# ---- sessionfs CLI ----
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS sfs
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/sessionfs/ src/sessionfs/
RUN pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=80.10" "wheel>=0.46.2" \
    && pip install --no-cache-dir .

# ---- runtime ----
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ARG TOOL

# git + CA roots only: some installers resolve a local marketplace through
# git; nothing else is needed (the harness reads /proc directly).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged tool user (works_in_harness.DEFAULT_TOOL_USER). No home:
# each job gets a throwaway HOME owned by this uid.
RUN groupadd --system --gid 10002 sfs-tool \
    && useradd --system --uid 10002 --gid sfs-tool --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin sfs-tool

# Same site-packages swap as the API image: the builder's upgraded pip/
# setuptools dist-info must not sit next to the base image's older ones.
RUN rm -rf /usr/local/lib/python3.12/site-packages
COPY --from=sfs /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=sfs /usr/local/bin/sfs /usr/local/bin/sfs
COPY --from=tool /opt/sfs-harness/tool /opt/sfs-harness/tool
COPY --from=tool /opt/sfs-harness/rootfs/ /
COPY deploy/harness/tools.json deploy/harness/selfcheck.py /opt/sfs-harness/
COPY --chmod=0555 deploy/harness/entrypoint.sh /usr/local/bin/sfs-harness-entrypoint

# The harness never runs pip; drop it (and its vendored libraries) like the
# API image does. Everything under /opt and /usr/local stays root-owned.
RUN python -m pip uninstall -y pip \
    && chown -R root:root /opt/sfs-harness \
    && chmod -R go-w /opt/sfs-harness

ENV SFS_HARNESS_TOOL=${TOOL} \
    SFS_HARNESS_TOOL_USER=sfs-tool \
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.title="sessionfs works-in harness (${TOOL})" \
      org.opencontainers.image.description="Runs one tool's own plugin installer against a SessionFS catalog projection (L1 works-in verification)" \
      dev.sessionfs.harness.tool="${TOOL}"

ENTRYPOINT ["/usr/local/bin/sfs-harness-entrypoint"]
