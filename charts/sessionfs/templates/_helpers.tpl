{{/*
Expand the name of the chart.
*/}}
{{- define "sessionfs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "sessionfs.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "sessionfs.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sessionfs.labels" -}}
helm.sh/chart: {{ include "sessionfs.chart" . }}
{{ include "sessionfs.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "sessionfs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sessionfs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component labels helper — call with (list $ "component-name")
*/}}
{{- define "sessionfs.componentLabels" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{ include "sessionfs.labels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end }}

{{/*
Component selector labels
*/}}
{{- define "sessionfs.componentSelectorLabels" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{ include "sessionfs.selectorLabels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end }}

{{/*
API image
*/}}
{{- define "sessionfs.api.image" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- $repo := .Values.api.image.repository -}}
{{- $tag := .Values.api.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end }}

{{/*
MCP image
*/}}
{{- define "sessionfs.mcp.image" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- $repo := .Values.mcp.image.repository -}}
{{- $tag := .Values.mcp.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end }}

{{/*
Dashboard image
*/}}
{{- define "sessionfs.dashboard.image" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- $repo := .Values.dashboard.image.repository -}}
{{- $tag := .Values.dashboard.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end }}

{{/*
PostgreSQL full name
*/}}
{{- define "sessionfs.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "sessionfs.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/*
Build the DATABASE_URL from components.
When built-in PostgreSQL is enabled, connect to the StatefulSet service.
Otherwise, use externalDatabase values.
*/}}
{{- define "sessionfs.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql+asyncpg://$(DATABASE_USER):$(DATABASE_PASSWORD)@{{ include "sessionfs.postgresql.fullname" . }}:5432/{{ .Values.postgresql.auth.database }}
{{- else -}}
postgresql+asyncpg://{{ .Values.externalDatabase.username }}:$(DATABASE_PASSWORD)@{{ .Values.externalDatabase.host }}:{{ .Values.externalDatabase.port }}/{{ .Values.externalDatabase.database }}?ssl={{ .Values.externalDatabase.sslMode }}
{{- end -}}
{{- end }}

{{/*
Alembic DATABASE_URL (uses psycopg2 sync driver for migrations).
*/}}
{{- define "sessionfs.migrationDatabaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql://$(DATABASE_USER):$(DATABASE_PASSWORD)@{{ include "sessionfs.postgresql.fullname" . }}:5432/{{ .Values.postgresql.auth.database }}
{{- else -}}
postgresql://{{ .Values.externalDatabase.username }}:$(DATABASE_PASSWORD)@{{ .Values.externalDatabase.host }}:{{ .Values.externalDatabase.port }}/{{ .Values.externalDatabase.database }}?sslmode={{ .Values.externalDatabase.sslMode }}
{{- end -}}
{{- end }}

{{/*
Database credential env vars — reused across API deployment and migration job.
*/}}
{{- define "sessionfs.databaseEnv" -}}
{{- if .Values.postgresql.enabled }}
- name: DATABASE_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgresql.auth.existingSecret | default (printf "%s-postgresql" (include "sessionfs.fullname" .)) }}
      key: {{ .Values.postgresql.auth.secretKeys.usernameKey }}
- name: DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.postgresql.auth.existingSecret | default (printf "%s-postgresql" (include "sessionfs.fullname" .)) }}
      key: {{ .Values.postgresql.auth.secretKeys.passwordKey }}
{{- else }}
- name: DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.externalDatabase.existingSecret | default (printf "%s-external-db" (include "sessionfs.fullname" .)) }}
      key: {{ .Values.externalDatabase.secretKey | default "database-password" }}
{{- end }}
{{- end }}

{{/*
Secrets name helper
*/}}
{{- define "sessionfs.secretsName" -}}
{{- .Values.security.existingSecret | default (printf "%s-secrets" (include "sessionfs.fullname" .)) -}}
{{- end }}

{{/*
Service account name
*/}}
{{- define "sessionfs.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- .Values.serviceAccount.name | default (include "sessionfs.fullname" .) }}
{{- else }}
{{- .Values.serviceAccount.name | default "default" }}
{{- end }}
{{- end }}

{{/*
Image pull secrets
*/}}
{{- define "sessionfs.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
