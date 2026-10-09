"""OpenTelemetry observability: traces, metrics and trace-correlated structured logs.

Configuration (environment):
  OTEL_EXPORTER_OTLP_ENDPOINT   OTLP/HTTP collector endpoint; enables export when set
  OTEL_SERVICE_NAME             defaults to "sdtf-api" / "sdtf-worker"
  SDTF_OTEL_ENABLED             "1"/"0" override (default: enabled when an endpoint is set)
  SDTF_OTEL_CONSOLE             "1" prints spans/metrics to stdout (development)
  SDTF_LOG_FORMAT               "json" (default in containers) or "text"

Everything degrades to no-ops when OpenTelemetry is disabled or not installed, so domain code can call
`span()`, `counter()` and `histogram()` unconditionally. Tests use `configure_in_memory()` to capture telemetry.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

try:
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry import trace as _otel_trace

    _HAVE_OTEL = True
except ImportError:  # pragma: no cover
    _HAVE_OTEL = False

_tracer = None
_meter = None
_instruments: dict[str, Any] = {}
_state = {"configured": False, "enabled": False, "exporter": None, "service": None}


@dataclass
class OtelConfig:
    service_name: str = field(default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "sdtf-api"))
    endpoint: str = field(default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    enabled: bool = field(default_factory=lambda: os.getenv("SDTF_OTEL_ENABLED", "") == "1" or (os.getenv("SDTF_OTEL_ENABLED", "") != "0" and bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""))))
    console: bool = field(default_factory=lambda: os.getenv("SDTF_OTEL_CONSOLE", "0") == "1")
    log_format: str = field(default_factory=lambda: os.getenv("SDTF_LOG_FORMAT", "text"))


# ----------------------------------------------------------------------------------------- setup
def setup(cfg: OtelConfig | None = None, service_name: str | None = None) -> dict:
    """Configure providers and exporters. Idempotent; safe to call when OTel is not installed."""
    global _tracer, _meter
    cfg = cfg or OtelConfig()
    if service_name:
        cfg.service_name = service_name
    configure_logging(cfg.log_format)
    if not _HAVE_OTEL or not (cfg.enabled or cfg.console):
        _state.update({"configured": True, "enabled": False, "exporter": None, "service": cfg.service_name})
        _tracer, _meter = None, None
        return dict(_state)
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: cfg.service_name, "service.namespace": "sdtf"})
    tp = TracerProvider(resource=resource)
    readers = []
    exporter = "none"
    if cfg.endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.endpoint.rstrip("/") + "/v1/traces")))
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=cfg.endpoint.rstrip("/") + "/v1/metrics")))
        exporter = "otlp-http"
    if cfg.console:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
        exporter = exporter + "+console" if exporter != "none" else "console"
    _install_providers(tp, MeterProvider(resource=resource, metric_readers=readers), exporter, cfg.service_name)
    return dict(_state)


def configure_in_memory(service_name: str = "sdtf-test"):
    """Testing hook: capture spans and metrics in memory. Returns (span_exporter, metric_reader)."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    resource = Resource.create({SERVICE_NAME: service_name})
    span_exporter = InMemorySpanExporter()
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    reader = InMemoryMetricReader()
    _install_providers(tp, MeterProvider(resource=resource, metric_readers=[reader]), "memory", service_name)
    return span_exporter, reader


def _install_providers(tracer_provider, meter_provider, exporter: str, service: str) -> None:
    """Providers can only be set once per process; later configurations reuse them through the tracer/meter
    objects we hold, which is enough for tests and for re-configuration with the same exporters."""
    global _tracer, _meter
    _instruments.clear()
    _tracer = tracer_provider.get_tracer("sdtf")
    _meter = meter_provider.get_meter("sdtf")
    try:
        _otel_trace.set_tracer_provider(tracer_provider)
        _otel_metrics.set_meter_provider(meter_provider)
    except Exception:  # noqa: BLE001 - provider already set (e.g. second test configuration); our handles still work
        pass
    _state.update({"configured": True, "enabled": True, "exporter": exporter, "service": service})


def disable() -> None:
    global _tracer, _meter
    _tracer, _meter = None, None
    _instruments.clear()
    _state.update({"configured": True, "enabled": False, "exporter": None})


def status() -> dict:
    return dict(_state)


# ----------------------------------------------------------------------------------------- tracing
class _NoopSpan:
    def set_attribute(self, *_a, **_k):
        return None

    def set_attributes(self, *_a, **_k):
        return None

    def record_exception(self, *_a, **_k):
        return None

    def set_status(self, *_a, **_k):
        return None


_NOOP = _NoopSpan()


@contextmanager
def span(name: str, **attributes):
    """Trace span context manager. Attributes with None values are dropped; dict/list values are JSON-encoded."""
    if _tracer is None:
        yield _NOOP
        return
    attrs = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in attributes.items() if v is not None}
    with _tracer.start_as_current_span(name, attributes=attrs) as s:
        try:
            yield s
        except Exception as e:  # noqa: BLE001 - re-raised after recording
            s.record_exception(e)
            from opentelemetry.trace import Status, StatusCode

            s.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def current_trace_ids() -> dict:
    if not _HAVE_OTEL or _tracer is None:
        return {}
    ctx = _otel_trace.get_current_span().get_span_context()
    if not ctx or not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


# ----------------------------------------------------------------------------------------- metrics
def counter(name: str, value: int | float = 1, **attributes) -> None:
    if _meter is None:
        return
    key = ("counter", name)
    if key not in _instruments:
        _instruments[key] = _meter.create_counter(name)
    _instruments[key].add(value, {k: str(v) for k, v in attributes.items() if v is not None})


def histogram(name: str, value: float, unit: str = "ms", **attributes) -> None:
    if _meter is None:
        return
    key = ("histogram", name)
    if key not in _instruments:
        _instruments[key] = _meter.create_histogram(name, unit=unit)
    _instruments[key].record(value, {k: str(v) for k, v in attributes.items() if v is not None})


@contextmanager
def timed(name: str, **attributes):
    """Span + duration histogram `<name>.duration` in milliseconds."""
    t0 = time.monotonic()
    with span(name, **attributes) as s:
        yield s
    histogram(f"{name}.duration", (time.monotonic() - t0) * 1000.0, **attributes)


# ----------------------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name, "msg": record.getMessage(), **current_trace_ids()}
        for k in ("run_id", "stage", "partition", "worker_id", "actor"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in current_trace_ids().items():
            setattr(record, k, v)
        return True


def configure_logging(fmt: str = "text") -> None:
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for h in root.handlers:
        if fmt == "json":
            h.setFormatter(JsonFormatter())
        else:
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s [trace=%(trace_id)s]"))
            h.addFilter(_DefaultTraceFilter())
    root.setLevel(os.getenv("SDTF_LOG_LEVEL", "INFO"))


class _DefaultTraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ids = current_trace_ids()
        record.trace_id = ids.get("trace_id", "-")
        return True


# ----------------------------------------------------------------------------------------- framework hooks
def instrument_app(app) -> bool:
    if _tracer is None:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
        return True
    except Exception:  # noqa: BLE001 - optional dependency
        return False


def instrument_engine(engine) -> bool:
    if _tracer is None:
        return False
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine, enable_commenter=False)
        return True
    except Exception:  # noqa: BLE001
        return False
