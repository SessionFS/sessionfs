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
Scheduled platform sweeps (GitHub #115). One entry per sweep drives both the
shared sweep-cronjob template and the NetworkPolicy ingress rules so the five
sweeps stay in lock-step. `key` names the api.<key> values section; `component`
is the k8s component label; `path` is the admin sweep endpoint; `schedule` and
`secretKey` are the cron defaults (override via api.<key>.cronJob).
*/}}
{{- define "sessionfs.sweepDefs" -}}
- key: ticketNotifications
  component: ticket-notifications
  path: /api/v1/admin/sweeps/ticket-notifications
  schedule: '*/10 * * * *'
  envVar: SFS_TICKET_NOTIFICATIONS_API_KEY
  secretKey: ticket-notifications-api-key
- key: orgDigest
  component: org-digest
  path: /api/v1/admin/sweeps/org-digest
  schedule: '0 4 * * *'
  envVar: SFS_ORG_DIGEST_API_KEY
  secretKey: org-digest-api-key
- key: kbCompile
  component: kb-compile
  path: /api/v1/admin/sweeps/kb-compile
  schedule: '*/15 * * * *'
  envVar: SFS_KB_COMPILE_API_KEY
  secretKey: kb-compile-api-key
- key: jiraSync
  component: jira-sync
  path: /api/v1/admin/sweeps/jira-sync
  schedule: '* * * * *'
  envVar: SFS_JIRA_SYNC_API_KEY
  secretKey: jira-sync-api-key
- key: confluencePublish
  component: confluence-publish
  path: /api/v1/admin/sweeps/confluence-publish
  schedule: '*/15 * * * *'
  envVar: SFS_CONFLUENCE_PUBLISH_API_KEY
  secretKey: confluence-publish-api-key
- key: meetingRetention
  component: meeting-retention
  path: /api/v1/admin/sweeps/meeting-retention
  schedule: '30 3 * * *'
  envVar: SFS_MEETING_RETENTION_API_KEY
  secretKey: meeting-retention-api-key
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
postgresql+asyncpg://{{ .Values.externalDatabase.username }}:$(DATABASE_PASSWORD)@{{ .Values.externalDatabase.host }}:{{ .Values.externalDatabase.port }}/{{ .Values.externalDatabase.database }}
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

{{/*
dashboard.apiProxyTimeout as whole seconds. It feeds both nginx (which takes
"300s") and ingress-nginx annotations (which take "300"), so only plain
seconds are accepted: "300" or "300s".
*/}}
{{- define "sessionfs.apiProxyTimeoutSeconds" -}}
{{- $value := .Values.dashboard.apiProxyTimeout -}}
{{- $raw := ternary "300s" (toString $value) (kindIs "invalid" $value) -}}
{{- if not (regexMatch "^[1-9][0-9]*s?$" $raw) -}}
{{- fail (printf "dashboard.apiProxyTimeout must be whole seconds such as \"300s\", got %q" $raw) -}}
{{- end -}}
{{- trimSuffix "s" $raw -}}
{{- end -}}
