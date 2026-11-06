{{- define "media-manager.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "media-manager.labels" -}}
app: {{ include "media-manager.fullname" . }}
{{- end -}}
