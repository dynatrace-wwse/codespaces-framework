# AstroShop Lite

Lightweight OpenTelemetry demo — 5 services in 5 languages (Next.js, Go, Java, Python, Rust),
PostgreSQL, Valkey and a k6 load generator. Runs on **amd64 and arm64** in well under 1 GiB.

- Original: [henrikrexed/opentelemetry-demo-light](https://github.com/henrikrexed/opentelemetry-demo-light)
  by **Henrik Rexed** — all credit for the app and its instrumentation is his.
- Dynatrace import: `dynatrace-wwse/astroshop-lite` (private), full upstream history.
- `chart/` is vendored from that import at commit `5580e03` (upstream v0.0.16 + arm64 readiness,
  non-root security context, cart + checkout memory, generated postgres password). `values.yaml` holds the framework overrides.

```bash
deployApp astroshop-lite          # or: deployApp 10 / deployApp j / deployAstroshopLite
deployApp astroshop-lite -d       # undeploy
```

Telemetry goes to the tenant through the framework collector (`deployOtelCollector`, needs
`DT_OTEL_ENDPOINT` + `DT_INGEST_TOKEN`). To send it somewhere else — e.g. a gateway collector a
training deploys itself — export the OTLP **gRPC** endpoint first:

```bash
ASTROSHOP_LITE_OTLP_ENDPOINT=http://my-gateway.my-ns.svc.cluster.local:4317 deployApp astroshop-lite
```

Not the same app as `astroshop` (the Dynatrace-curated full OpenTelemetry demo, amd64 only).
