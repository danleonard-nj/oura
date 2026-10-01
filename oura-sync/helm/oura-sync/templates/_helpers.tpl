{{/*
Expand the name of the chart.
*/}}
{{- define "oura-sync.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, capped at 63 characters for the DNS label limit.
*/}}
{{- define "oura-sync.fullname" -}}
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

{{- define "oura-sync.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "oura-sync.labels" -}}
helm.sh/chart: {{ include "oura-sync.chart" . }}
{{ include "oura-sync.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "oura-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oura-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "oura-sync.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "oura-sync.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding the application credentials: an externally managed
one when `existingSecret` is set, otherwise the one this chart renders.
*/}}
{{- define "oura-sync.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "oura-sync.fullname" . }}
{{- end }}
{{- end }}

{{/*
True when at least one secret value is supplied inline, meaning the chart
should render its own Secret.
*/}}
{{- define "oura-sync.createSecret" -}}
{{- if .Values.existingSecret }}
{{- "false" }}
{{- else if or .Values.secrets.ouraClientId .Values.secrets.ouraClientSecret .Values.secrets.ouraRedirectUri .Values.secrets.tokenEncryptionKey .Values.secrets.mongodbUri .Values.secrets.redisUrl .Values.secrets.aadTenantId .Values.secrets.aadAudience .Values.secrets.aadRequiredRole }}
{{- "true" }}
{{- else }}
{{- "false" }}
{{- end }}
{{- end }}
