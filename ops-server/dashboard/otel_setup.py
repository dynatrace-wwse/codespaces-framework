"""OpenTelemetry wiring for Orbital services — direct OTLP export to the COE tenant.

No OneAgent on this host BY DESIGN: OneAgent also instrumented the Sysbox
provisioning containers and the dev tooling running on the master, polluting the
tenant. This module instruments ONLY our own processes (FastAPI, Redis client,
httpx) and ships host gauges, so the COE tenant sees Orbital's backend
transactions and nothing else.

Enabled when DT_INGEST_TOKEN is present (dt0c01 with OTLP ingest scopes, already
in /home/ops/.env). Override the target with OTEL_EXPORT_BASE; disable entirely
with OTEL_DISABLED=1.
"""

import logging
import os
import socket

log = logging.getLogger("ops-otel")

_initialized = False

OTEL_BASE_DEFAULT = "https://geu80787.live.dynatrace.com/api/v2/otlp"


def init_otel(app=None, service_name: str = "orbital-dashboard") -> bool:
    """Set up tracing + metrics providers and auto-instrumentation. Idempotent.
    Returns True when telemetry is active."""
    global _initialized
    token = os.environ.get("DT_INGEST_TOKEN", "")
    if os.environ.get("OTEL_DISABLED") == "1" or not token:
        log.info("OTel disabled (no DT_INGEST_TOKEN or OTEL_DISABLED=1)")
        return False
    base = os.environ.get("OTEL_EXPORT_BASE", OTEL_BASE_DEFAULT).rstrip("/")
    headers = {"Authorization": f"Api-Token {token}"}

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if not _initialized:
        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": "orbital",
            "host.name": socket.gethostname(),
            "deployment.environment": "production",
        })

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers))
        )
        trace.set_tracer_provider(tracer_provider)

        # Dynatrace requires DELTA temporality for counters/histograms.
        exporter = OTLPMetricExporter(
            endpoint=f"{base}/v1/metrics",
            headers=headers,
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            },
        )
        metrics.set_meter_provider(MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)],
        ))

        RedisInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        _register_host_gauges()
        _initialized = True
        log.info("OTel active → %s (service=%s)", base, service_name)

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        # /health & /status are poll noise; oauth2-proxy auth sub-requests add nothing.
        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health,/status,/oauth2/.*")
    return True


def _register_host_gauges() -> None:
    """Host + own-process gauges via psutil (the master has no OneAgent)."""
    import psutil
    from opentelemetry import metrics

    meter = metrics.get_meter("orbital.host")
    proc = psutil.Process()

    def cpu(_):  yield metrics.Observation(psutil.cpu_percent(interval=None))
    def mem(_):  yield metrics.Observation(psutil.virtual_memory().percent)
    def disk(_): yield metrics.Observation(psutil.disk_usage("/").percent)
    def load(_): yield metrics.Observation(os.getloadavg()[0])
    def prss(_): yield metrics.Observation(proc.memory_info().rss / 1048576)
    def pcpu(_): yield metrics.Observation(proc.cpu_percent(interval=None))

    meter.create_observable_gauge("orbital.host.cpu.percent", callbacks=[cpu], unit="%")
    meter.create_observable_gauge("orbital.host.mem.percent", callbacks=[mem], unit="%")
    meter.create_observable_gauge("orbital.host.disk.percent", callbacks=[disk], unit="%")
    meter.create_observable_gauge("orbital.host.load1", callbacks=[load])
    meter.create_observable_gauge("orbital.process.rss_mib", callbacks=[prss], unit="MiB")
    meter.create_observable_gauge("orbital.process.cpu.percent", callbacks=[pcpu], unit="%")
