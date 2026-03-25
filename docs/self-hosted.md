# Self-Hosted Deployment

Deploy SessionFS on your own Kubernetes cluster.

## Overview

SessionFS can be deployed to any Kubernetes cluster using the official Helm chart. The deployment includes:

- **API Server** -- FastAPI application handling session CRUD, sync, and authentication
- **MCP Server** -- Model Context Protocol bridge (optional)
- **Web Dashboard** -- React management interface (optional)
- **PostgreSQL** -- Built-in or external database
- **Blob Storage** -- Local PVC, Amazon S3, or Google Cloud Storage

## Prerequisites

- Kubernetes 1.26 or later
- Helm 3.12 or later
- `kubectl` configured for your cluster
- A PersistentVolume provisioner (most managed clusters include one)

## Installation

### 1. Add the Helm Repository

```bash
helm repo add sessionfs https://charts.sessionfs.dev
helm repo update
```

### 2. Create a Namespace

```bash
kubectl create namespace sessionfs
```

### 3. Choose Your Configuration

#### Minimal (development / evaluation)

Single replica, built-in PostgreSQL, no ingress:

```bash
helm install sessionfs sessionfs/sessionfs \
  -f values.minimal.yaml \
  --namespace sessionfs
```

Access via port-forward:

```bash
kubectl port-forward svc/sessionfs-api 8000:8000 -n sessionfs
```

#### Standard

Two API replicas, built-in PostgreSQL, ingress enabled:

```bash
helm install sessionfs sessionfs/sessionfs \
  --namespace sessionfs \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com
```

#### Production

External database, cloud storage, autoscaling, network policies:

```bash
helm install sessionfs sessionfs/sessionfs \
  -f values.production.yaml \
  --namespace sessionfs \
  --set postgresql.enabled=false \
  --set externalDatabase.existingSecret=sessionfs-db \
  --set security.existingSecret=sessionfs-secrets \
  --set storage.type=s3 \
  --set storage.s3.bucket=my-sessionfs-bucket \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com \
  --set ingress.hosts[0].paths.api=/api \
  --set ingress.hosts[0].paths.mcp=/mcp \
  --set ingress.hosts[0].paths.dashboard=/
```

## Secrets Management

SessionFS requires several secrets for operation. You can either provide them inline in `values.yaml` (not recommended for production) or reference pre-existing Kubernetes secrets.

### Create Secrets Manually

```bash
# Application secrets
kubectl create secret generic sessionfs-secrets \
  --namespace sessionfs \
  --from-literal=verification-secret="$(openssl rand -hex 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=resend-api-key="re_your_key_here"

# External database (if not using built-in PostgreSQL)
kubectl create secret generic sessionfs-db \
  --namespace sessionfs \
  --from-literal=database-url="postgresql+asyncpg://user:pass@host:5432/sessionfs"
```

Then reference them in your Helm values:

```yaml
security:
  existingSecret: sessionfs-secrets
externalDatabase:
  existingSecret: sessionfs-db
```

## Storage Configuration

### Local (default)

Uses a PersistentVolumeClaim. Suitable for single-node clusters or evaluation.

```yaml
storage:
  type: local
  local:
    persistence:
      enabled: true
      size: 10Gi
```

### Amazon S3

```yaml
storage:
  type: s3
  s3:
    bucket: my-sessionfs-bucket
    region: us-east-1
```

If your nodes use IAM roles for service accounts (IRSA), no additional credentials are needed. Otherwise, create a secret:

```bash
kubectl create secret generic aws-creds \
  --namespace sessionfs \
  --from-literal=aws-access-key-id=AKIA... \
  --from-literal=aws-secret-access-key=...
```

```yaml
storage:
  s3:
    existingSecret: aws-creds
```

### Google Cloud Storage

```yaml
storage:
  type: gcs
  gcs:
    bucket: my-sessionfs-bucket
```

If using Workload Identity, no additional credentials are needed. Otherwise:

```bash
kubectl create secret generic gcs-creds \
  --namespace sessionfs \
  --from-file=gcs-credentials-json=./sa-key.json
```

```yaml
storage:
  gcs:
    existingSecret: gcs-creds
```

## Database Configuration

### Built-in PostgreSQL (default)

The chart deploys a single-replica PostgreSQL StatefulSet. Suitable for small deployments.

```yaml
postgresql:
  enabled: true
  auth:
    username: sessionfs
    database: sessionfs
  persistence:
    size: 10Gi
```

### External Database

For production, use a managed PostgreSQL service (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL).

```yaml
postgresql:
  enabled: false

externalDatabase:
  host: your-db-host.region.rds.amazonaws.com
  port: 5432
  username: sessionfs
  database: sessionfs
  sslMode: require
  existingSecret: sessionfs-db
```

## TLS / HTTPS

Configure TLS through your ingress controller. Example with cert-manager:

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: sessionfs.yourdomain.com
      paths:
        api: /api
        mcp: /mcp
        dashboard: /
  tls:
    - secretName: sessionfs-tls
      hosts:
        - sessionfs.yourdomain.com
```

## Monitoring

Enable Prometheus ServiceMonitor (requires prometheus-operator):

```yaml
monitoring:
  serviceMonitor:
    enabled: true
    interval: 30s
    labels:
      release: prometheus
```

## Upgrading

```bash
helm repo update
helm upgrade sessionfs sessionfs/sessionfs \
  --namespace sessionfs \
  --reuse-values
```

Database migrations run automatically as a Helm post-upgrade hook.

## Troubleshooting

### Check pod status

```bash
kubectl get pods -n sessionfs
kubectl describe pod <pod-name> -n sessionfs
```

### View API logs

```bash
kubectl logs -n sessionfs -l app.kubernetes.io/component=api --tail=100
```

### Check migration job

```bash
kubectl get jobs -n sessionfs -l app.kubernetes.io/component=migration
kubectl logs -n sessionfs job/sessionfs-migrate-<revision>
```

### Run Helm tests

```bash
helm test sessionfs --namespace sessionfs
```

### Common Issues

**Pods stuck in Pending:** Check that your cluster has a PersistentVolume provisioner and sufficient resources.

**Database connection errors:** Verify the database URL and credentials. For external databases, ensure network connectivity (security groups, VPC peering).

**Ingress not working:** Confirm your ingress controller is installed and the ingress class name matches.
