{{/*
Expand the name of the chart.
*/}}
{{- define "oura-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, capped at 63 characters for the DNS label limit.
*/}}
{{- define "oura-mcp.fullname" -}}
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

{{- define "oura-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "oura-mcp.labels" -}}
helm.sh/chart: {{ include "oura-mcp.chart" . }}
{{ include "oura-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "oura-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oura-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "oura-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "oura-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding the application credentials: an externally managed
one when `existingSecret` is set, otherwise the one this chart renders.
*/}}
{{- define "oura-mcp.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "oura-mcp.fullname" . }}
{{- end }}
{{- end }}

{{/*
True when at least one secret value is supplied inline, meaning the chart
should render its own Secret.
*/}}
{{- define "oura-mcp.createSecret" -}}
{{- if .Values.existingSecret }}
{{- "false" }}
{{- else if or .Values.secrets.ouraClientId .Values.secrets.ouraClientSecret .Values.secrets.mcpApiKey .Values.secrets.tokenEncryptionKey .Values.secrets.mongodbUri .Values.secrets.redisUrl .Values.secrets.ouraWebhookVerificationToken }}
{{- "true" }}
{{- else }}
{{- "false" }}
{{- end }}
{{- end }}

{{/*
The hostname the application advertises. Falls back to the ingress hostname so
the MCP transport's allowed-hosts list is correct without repeating it.
*/}}
{{- define "oura-mcp.publicHost" -}}
{{- if .Values.config.publicHost }}
{{- .Values.config.publicHost }}
{{- else if .Values.ingress.enabled }}
{{- .Values.ingress.hostname }}
{{- end }}
{{- end }}

{{/*
The OAuth redirect URI, derived from the ingress hostname when not set
explicitly. It must match what is registered on the Oura application.
*/}}
{{- define "oura-mcp.redirectUri" -}}
{{- if .Values.config.ouraRedirectUri }}
{{- .Values.config.ouraRedirectUri }}
{{- else if .Values.ingress.enabled }}
{{- printf "https://%s/oauth/callback" .Values.ingress.hostname }}
{{- end }}
{{- end }}
