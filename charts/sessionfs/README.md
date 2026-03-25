# SessionFS Helm Chart

Deploy SessionFS on Kubernetes -- capture, sync, and resume AI coding sessions across tools and teammates.

## Prerequisites

- Kubernetes 1.26+
- Helm 3.12+
- PV provisioner (for PostgreSQL persistence)
- Ingress controller (nginx-ingress or similar, optional)

## Quick Start (Minimal)

Deploy a single-replica instance with built-in PostgreSQL and no ingress:

```bash
helm repo add sessionfs https://charts.sessionfs.dev
helm repo update

helm install sessionfs sessionfs/sessionfs \
  -f values.minimal.yaml \
  --namespace sessionfs \
  --create-namespace
```

Access via port-forward:

```bash
kubectl port-forward svc/sessionfs-api 8000:8000 -n sessionfs
curl http://localhost:8000/health
```

## Production Deployment

### AWS EKS + RDS + S3

1. Create an RDS PostgreSQL instance and an S3 bucket.

2. Create a Kubernetes secret with your credentials:

```bash
kubectl create namespace sessionfs

kubectl create secret generic sessionfs-db \
  --namespace sessionfs \
  --from-literal=database-url="postgresql+asyncpg://user:pass@your-rds-host:5432/sessionfs?ssl=require"

kubectl create secret generic sessionfs-secrets \
  --namespace sessionfs \
  --from-literal=verification-secret="$(openssl rand -hex 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=resend-api-key="re_your_key_here"
```

3. Install with production values:

```bash
helm install sessionfs sessionfs/sessionfs \
  -f values.production.yaml \
  --namespace sessionfs \
  --set postgresql.enabled=false \
  --set externalDatabase.existingSecret=sessionfs-db \
  --set security.existingSecret=sessionfs-secrets \
  --set storage.type=s3 \
  --set storage.s3.bucket=your-sessionfs-bucket \
  --set storage.s3.region=us-east-1 \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com \
  --set ingress.hosts[0].paths.api=/api \
  --set ingress.hosts[0].paths.mcp=/mcp \
  --set ingress.hosts[0].paths.dashboard=/
```

### GCP GKE + Cloud SQL + GCS

1. Create a Cloud SQL PostgreSQL instance and a GCS bucket.

2. Create Kubernetes secrets:

```bash
kubectl create namespace sessionfs

kubectl create secret generic sessionfs-db \
  --namespace sessionfs \
  --from-literal=database-url="postgresql+asyncpg://user:pass@cloud-sql-ip:5432/sessionfs?ssl=require"

kubectl create secret generic sessionfs-secrets \
  --namespace sessionfs \
  --from-literal=verification-secret="$(openssl rand -hex 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=resend-api-key="re_your_key_here"

kubectl create secret generic gcs-credentials \
  --namespace sessionfs \
  --from-file=gcs-credentials-json=./service-account.json
```

3. Install:

```bash
helm install sessionfs sessionfs/sessionfs \
  -f values.production.yaml \
  --namespace sessionfs \
  --set postgresql.enabled=false \
  --set externalDatabase.existingSecret=sessionfs-db \
  --set security.existingSecret=sessionfs-secrets \
  --set storage.type=gcs \
  --set storage.gcs.bucket=your-sessionfs-bucket \
  --set storage.gcs.existingSecret=gcs-credentials \
  --set ingress.enabled=true \
  --set ingress.className=gce \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com \
  --set ingress.hosts[0].paths.api=/api \
  --set ingress.hosts[0].paths.mcp=/mcp \
  --set ingress.hosts[0].paths.dashboard=/
```

## Configuration Reference

| Parameter | Description | Default |
|-----------|-------------|---------|
| `global.imageRegistry` | Container image registry | `ghcr.io/sessionfs` |
| `global.imagePullPolicy` | Image pull policy | `IfNotPresent` |
| `api.replicaCount` | API server replicas | `2` |
| `api.image.tag` | API image tag | `appVersion` |
| `api.service.port` | API service port | `8000` |
| `api.autoscaling.enabled` | Enable HPA for API | `false` |
| `api.autoscaling.maxReplicas` | Max API replicas | `10` |
| `api.logLevel` | Application log level | `INFO` |
| `api.corsOrigins` | Allowed CORS origins | `""` |
| `api.rateLimitPerMinute` | Rate limit per minute | `100` |
| `api.podDisruptionBudget.enabled` | Enable PDB | `true` |
| `mcp.enabled` | Deploy MCP server | `true` |
| `mcp.replicaCount` | MCP server replicas | `1` |
| `mcp.service.port` | MCP service port | `3001` |
| `dashboard.enabled` | Deploy web dashboard | `true` |
| `dashboard.replicaCount` | Dashboard replicas | `1` |
| `postgresql.enabled` | Deploy built-in PostgreSQL | `true` |
| `postgresql.auth.username` | PostgreSQL username | `sessionfs` |
| `postgresql.auth.database` | PostgreSQL database | `sessionfs` |
| `postgresql.persistence.size` | PostgreSQL PVC size | `10Gi` |
| `externalDatabase.host` | External DB host | `""` |
| `externalDatabase.port` | External DB port | `5432` |
| `externalDatabase.sslMode` | External DB SSL mode | `require` |
| `storage.type` | Blob storage type | `local` |
| `storage.s3.bucket` | S3 bucket name | `""` |
| `storage.s3.region` | S3 region | `us-east-1` |
| `storage.gcs.bucket` | GCS bucket name | `""` |
| `ingress.enabled` | Enable ingress | `true` |
| `ingress.className` | Ingress class name | `""` |
| `email.resendApiKey` | Resend API key | `""` |
| `security.networkPolicies.enabled` | Enable network policies | `false` |
| `security.podSecurityStandards.enforce` | PSS enforcement level | `baseline` |
| `migrations.enabled` | Run DB migrations on install/upgrade | `true` |
| `monitoring.serviceMonitor.enabled` | Enable Prometheus ServiceMonitor | `false` |

## Upgrading

### To 0.4.0

First release of the Helm chart. No upgrade path needed.

### General Upgrade Process

```bash
helm repo update
helm upgrade sessionfs sessionfs/sessionfs \
  --namespace sessionfs \
  --reuse-values
```

Database migrations run automatically as a post-upgrade hook. Monitor the migration job:

```bash
kubectl get jobs -n sessionfs -l app.kubernetes.io/component=migration
kubectl logs job/sessionfs-migrate-<revision> -n sessionfs
```

## Uninstalling

```bash
helm uninstall sessionfs --namespace sessionfs
```

Note: PersistentVolumeClaims for PostgreSQL data are not deleted automatically. Remove them manually if you want to delete all data:

```bash
kubectl delete pvc -n sessionfs -l app.kubernetes.io/component=postgresql
```
