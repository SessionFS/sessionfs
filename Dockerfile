FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
# The base image ships older pip/setuptools/wheel with published CVEs (and
# setuptools vendors jaraco.context). Upgrade them before installing.
RUN pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=80.10" "wheel>=0.46.2" \
    && pip install --no-cache-dir ".[server]"

FROM python:3.11-slim AS runtime

# Non-root user matching the Helm chart's runAsUser (10001). Cloud Run and
# Kubernetes already enforce non-root at the orchestration layer; baking it
# into the image itself also makes self-hosted users compliant by default.
RUN groupadd --system --gid 10001 sessionfs \
    && useradd --system --uid 10001 --gid sessionfs --no-create-home --shell /usr/sbin/nologin sessionfs

# git for server-side polling of Git marketplace registries (GitHub #69).
# Only the poller runs it (replaced environment, hooks off, bare repo, no
# checkout). >= 2.44 is required (GIT_NO_LAZY_FETCH); the build fails on an
# older distribution git rather than shipping a poller that refuses to run.
# openssh-client: the scheduled public-catalog mirror job (`sfs catalog
# export --ssh-key-file`) pushes to GitHub over SSH with a deploy key; git
# needs the ssh binary for that (--no-install-recommends keeps xauth out).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && git --version | awk '{ split($3, v, "."); if (v[1] < 2 || (v[1] == 2 && v[2] < 44)) { print "git >= 2.44 required, found " $3; exit 1 } }'

# Replace this stage's site-packages with the builder's rather than merging
# into it. COPY merges directories, so the base image's older pip/setuptools/
# wheel dist-info would otherwise survive next to the upgraded versions (and
# a cached layer here could drift from the builder's resolved versions).
RUN rm -rf /usr/local/lib/python3.11/site-packages

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/alembic /usr/local/bin/
COPY src/ src/
COPY alembic.ini .

# The server never runs pip, and pip vendors its own copies of urllib3 and
# msgpack that lag the fixed releases (even on the newest pip). Remove it
# from the runtime image so those copies don't ship or trip the gate.
RUN python -m pip uninstall -y pip \
    && chown -R sessionfs:sessionfs /app

USER 10001

EXPOSE 8000

CMD ["uvicorn", "sessionfs.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
