{{/*
Common labels
*/}}
{{- define "otel-demo.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: opentelemetry-demo-light
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/*
Selector labels for a component
*/}}
{{- define "otel-demo.selectorLabels" -}}
app: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{/*
OTLP endpoint — collector internal address or user-provided
*/}}
{{- define "otel-demo.otlpEndpoint" -}}
{{- if .Values.collector.enabled -}}
http://{{ .Release.Name }}-otel-collector:4317
{{- else -}}
{{ .Values.otlp.endpoint }}
{{- end -}}
{{- end }}

{{/*
Common OTel environment variables injected into every app service
*/}}
{{- define "otel-demo.otelEnv" -}}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ include "otel-demo.otlpEndpoint" . | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: "grpc"
- name: OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE
  value: {{ .Values.metrics.aggregation | quote }}
- name: PRODUCT_CATALOG_ADDR
  value: "{{ .Release.Name }}-product-catalog:{{ .Values.productCatalog.port }}"
- name: CART_ADDR
  value: "{{ .Release.Name }}-cart:{{ .Values.cart.port }}"
- name: CHECKOUT_ADDR
  value: "{{ .Release.Name }}-checkout:{{ .Values.checkout.port }}"
- name: PAYMENT_ADDR
  value: "{{ .Release.Name }}-payment:{{ .Values.payment.port }}"
- name: POSTGRES_HOST
  value: "{{ .Release.Name }}-postgres"
- name: POSTGRES_PORT
  value: "5432"
- name: POSTGRES_USER
  value: {{ .Values.postgres.user | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-postgres
      key: password
- name: VALKEY_ADDR
  value: "{{ .Release.Name }}-valkey:6379"
{{- end }}

{{/*
Image spec for a service
Usage: {{ include "otel-demo.image" (dict "img" .Values.images.frontend) }}
*/}}
{{- define "otel-demo.image" -}}
{{ .img.repository }}:{{ .img.tag }}
{{- end }}

{{/*
Postgres password: postgres.password when set, else the one already in the
release's Secret (kept stable across upgrades), else a fresh random one.
*/}}
{{- define "otel-demo.postgresPassword" -}}
{{- if .Values.postgres.password -}}
{{ .Values.postgres.password }}
{{- else -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres" .Release.Name) -}}
{{- if and $existing $existing.data (hasKey $existing.data "password") -}}
{{ index $existing.data "password" | b64dec }}
{{- else -}}
{{ randAlphaNum 24 }}
{{- end -}}
{{- end -}}
{{- end }}
