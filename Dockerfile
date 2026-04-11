FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[server]"

FROM python:3.11-slim AS runtime

# Non-root user matching the Helm chart's runAsUser (10001). Cloud Run and
# Kubernetes already enforce non-root at the orchestration layer; baking it
# into the image itself also makes self-hosted users compliant by default.
RUN groupadd --system --gid 10001 sessionfs \
    && useradd --system --uid 10001 --gid sessionfs --no-create-home --shell /usr/sbin/nologin sessionfs

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/alembic /usr/local/bin/
COPY src/ src/
COPY alembic.ini .

RUN chown -R sessionfs:sessionfs /app

USER 10001

EXPOSE 8000

CMD ["uvicorn", "sessionfs.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
