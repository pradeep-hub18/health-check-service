{{- define "health-check-service.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 44 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (.Values.nameOverride | default .Chart.Name) | trunc 44 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "health-check-service.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "health-check-service.secretName" -}}
{{- .Values.database.existingSecret | default (printf "%s-postgres-secret" (include "health-check-service.fullname" .)) -}}
{{- end -}}
