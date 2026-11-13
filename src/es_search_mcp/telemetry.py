"""OpenTelemetry wiring: traces, metrics and logs out to Elastic APM.

FastMCP already instruments itself — it opens a SERVER span per MCP request,
tags it with `mcp.method.name` / `gen_ai.tool.name`, records exceptions, and
propagates trace context in and out through request `_meta`. It does all of that
against the OpenTelemetry **API** only, which is a no-op until some application
installs an SDK. This module is that application half: it builds the providers
and points them at a collector, so the spans FastMCP was already describing
start being recorded and exported.

Three signals leave the process:

``traces``
    FastMCP's MCP spans, the ASGI span for the inbound HTTP request, and an
    httpx client span per backend call — so one trace shows the whole path from
    an MCP ``tools/call`` down to ``POST /retrieve-rrf``.
``metrics``
    The instruments in :mod:`es_search_mcp.metrics`, pushed periodically.
``logs``
    The structured records from :mod:`es_search_mcp.logging`, carrying the trace
    context of the call that produced them, so a log line in APM links back to
    its span.

Everything the OpenTelemetry specification already has a convention for — the
endpoint, headers, protocol, sampling, resource attributes — is read from the
standard ``OTEL_*`` environment variables by the SDK, and is not re-invented
here. For Elastic APM that means:

    OTEL_EXPORTER_OTLP_ENDPOINT=https://<deployment>.apm.<region>.cloud.es.io:443
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=ApiKey%20<base64 key>

Export is off unless one of those endpoint variables is set, so a deployment
that has not been pointed at a collector stays silent rather than retrying into
the void.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# `_logs` is still marked provisional by the SDK, but it is the only
# stdlib-to-OTLP log bridge there is: the replacement the deprecation warning
# points at (a handler in opentelemetry-instrumentation-logging) does not exist
# in 0.66b2, which exports only `LoggingInstrumentor`.
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from es_search_mcp.config import Settings
from es_search_mcp.logging import LOGGER_NAMESPACE, get_logger

logger = get_logger(__name__)

#: Version reported as `service.version`. Kept here rather than imported from
#: the package root so this module stays free of a circular import.
SERVICE_VERSION = "0.1.0"

#: Opt-in that makes the HTTP instrumentations emit current semantic
#: conventions (`http.request.method`, `url.full`, `http.response.status_code`)
#: instead of the pre-1.0 names they still default to. Set as a default, so an
#: operator who has already chosen a value keeps it, and set from here rather
#: than left to the deployment because it has to match the attribute names this
#: service puts on its own metrics — a dashboard cannot join `http.method` on
#: one signal to `http.request.method` on another. The instrumentations read it
#: once, lazily, when first instrumented, which is after this module runs.
_SEMCONV_OPT_IN = ("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

#: Environment variables that, when set, mean "a collector is configured".
_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
)


def use_stable_http_semconv() -> None:
    """Opt the HTTP instrumentations in to current semantic conventions.

    They are read once, lazily, the first time something is instrumented, so
    this has to run before :func:`instrument_http_client` and before the ASGI
    middleware is built — which is why :func:`configure_telemetry` calls it
    first, whether or not export is switched on.
    """
    os.environ.setdefault(*_SEMCONV_OPT_IN)


def collector_is_configured() -> bool:
    """Whether any OTLP endpoint variable is set to a non-blank value."""
    return any(os.environ.get(name, "").strip() for name in _ENDPOINT_VARS)


def telemetry_is_enabled(settings: Settings) -> bool:
    """Resolve the three-state ``telemetry_enabled`` setting."""
    if settings.telemetry_enabled is not None:
        return settings.telemetry_enabled
    return collector_is_configured()


def _flatten(prefix: str, value: Any, into: dict[str, Any]) -> None:
    """Flatten one structured field into OTLP-compatible attributes.

    OTLP attribute values must be primitives or homogeneous sequences of them,
    so the nested ``details`` dicts our errors carry have to be spread into
    dotted keys. Anything that still does not fit is stringified rather than
    dropped — a value rendered as text is worth more during an incident than a
    missing one.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}", item, into)
    elif value is None or isinstance(value, (str, bool, int, float)):
        into[prefix] = value
    elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        into[prefix] = list(value)
    else:
        into[prefix] = str(value)


class StructuredLoggingHandler(LoggingHandler):
    """OTLP log handler that spreads our ``fields`` mapping into attributes.

    :class:`~es_search_mcp.logging.StructuredLogger` puts every structured field
    under a single ``fields`` dict on the record. Left alone the base handler
    would hand that dict to OTLP as one attribute value, which is not a type
    OTLP accepts, so the whole payload would be dropped. Flattening it here
    keeps each field queryable in APM on its own.
    """

    @staticmethod
    def _get_attributes(record: logging.LogRecord) -> dict[str, Any]:
        attributes = dict(LoggingHandler._get_attributes(record) or {})
        nested = attributes.pop("fields", None)
        if isinstance(nested, dict):
            for key, value in nested.items():
                _flatten(key, value, attributes)
        return attributes


@dataclass
class Telemetry:
    """Handle on the configured providers, used to shut them down cleanly."""

    enabled: bool = False
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    logger_provider: LoggerProvider | None = None
    log_handler: logging.Handler | None = None

    def shutdown(self) -> None:
        """Flush and stop every provider.

        Called from the server lifespan: the span and log processors batch, so
        without this the last few seconds of a run are lost when the process
        exits — exactly the seconds that matter when a container is being
        restarted because something went wrong.
        """
        if not self.enabled:
            return

        # Detach the log handler first: a provider being shut down must not be
        # asked to record the log lines produced by its own shutdown.
        if self.log_handler is not None:
            logging.getLogger(LOGGER_NAMESPACE).removeHandler(self.log_handler)
            self.log_handler = None

        for signal, provider in (
            ("traces", self.tracer_provider),
            ("metrics", self.meter_provider),
            ("logs", self.logger_provider),
        ):
            if provider is None:
                continue
            try:
                provider.shutdown()
            except Exception:  # pragma: no cover - best-effort flush
                logger.warning("telemetry.shutdown_failed", fields={"signal": signal})

        global _active
        if _active is self:
            _active = None
        self.enabled = False


#: The handle installed by :func:`configure_telemetry`, if any. Providers,
#: exporters and the log handler are process-wide, so configuring twice would
#: double every export; this makes a second call return the first result.
_active: Telemetry | None = None


def active_telemetry() -> Telemetry | None:
    """Return the handle installed by :func:`configure_telemetry`, if any."""
    return _active


def environment_declared_keys() -> set[str]:
    """Resource attribute keys the environment has already set.

    ``Resource.create`` gives the attributes passed to it precedence over the
    ones it detects from ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES``.
    That is backwards for a deployed service: an operator who sets
    ``OTEL_SERVICE_NAME`` means it. Knowing which keys they declared lets
    :func:`build_resource` leave exactly those alone.
    """
    declared: set[str] = set()
    if os.environ.get("OTEL_SERVICE_NAME", "").strip():
        declared.add("service.name")
    for pair in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, separator, _ = pair.partition("=")
        if separator and key.strip():
            declared.add(key.strip())
    return declared


def build_resource(settings: Settings) -> Resource:
    """Describe this service to APM.

    The application's values are defaults: any attribute the deployment set
    through ``OTEL_SERVICE_NAME`` or ``OTEL_RESOURCE_ATTRIBUTES`` is left to the
    environment, so a canary or a renamed service does not need a code change.
    """
    defaults: dict[str, Any] = {
        "service.name": settings.telemetry_service_name,
        "service.version": SERVICE_VERSION,
    }
    if settings.telemetry_environment:
        # Elastic reads this as the APM "environment" selector. Both spellings
        # are set: the second is pre-1.27 semconv, still what some Elastic
        # versions look for.
        defaults["deployment.environment.name"] = settings.telemetry_environment
        defaults["deployment.environment"] = settings.telemetry_environment

    declared = environment_declared_keys()
    return Resource.create({key: value for key, value in defaults.items() if key not in declared})


def configure_telemetry(settings: Settings) -> Telemetry:
    """Install the OpenTelemetry SDK, or return a disabled handle.

    Call this once, during server construction, and **after**
    :func:`es_search_mcp.logging.configure_logging` — the log bridge attaches to
    the namespace logger that function sets up.

    Calling it again returns the handle from the first call rather than
    installing a second set of providers: exporters and the log handler are
    process-wide, so configuring twice would send everything twice.

    Returns:
        A :class:`Telemetry` handle. When export is off every field is unset and
        :meth:`Telemetry.shutdown` does nothing, so callers need no branch.
    """
    global _active

    # Before the enablement check: naming has to be settled before anything is
    # instrumented, and it costs nothing when export is off.
    use_stable_http_semconv()

    if _active is not None and _active.enabled:
        return _active

    if not telemetry_is_enabled(settings):
        reason = (
            "no OTLP endpoint configured"
            if settings.telemetry_enabled is None
            else "disabled by setting"
        )
        logger.debug("telemetry.disabled", fields={"reason": reason})
        return Telemetry(enabled=False)

    resource = build_resource(settings)
    telemetry = Telemetry(enabled=True)

    _configure_traces(settings, resource, telemetry)
    _configure_metrics(settings, resource, telemetry)
    if settings.telemetry_export_logs:
        _configure_logs(resource, telemetry)

    _active = telemetry
    logger.info(
        "telemetry.configured",
        fields={
            "service_name": settings.telemetry_service_name,
            "environment": settings.telemetry_environment,
            "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            "logs_exported": settings.telemetry_export_logs,
            "metric_interval_ms": settings.telemetry_metric_interval_ms,
        },
    )
    return telemetry


def _configure_traces(settings: Settings, resource: Resource, telemetry: Telemetry) -> None:
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    if settings.telemetry_console_export:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    telemetry.tracer_provider = provider


def _configure_metrics(settings: Settings, resource: Resource, telemetry: Telemetry) -> None:
    readers = [
        PeriodicExportingMetricReader(
            OTLPMetricExporter(),
            export_interval_millis=settings.telemetry_metric_interval_ms,
        )
    ]
    if settings.telemetry_console_export:
        readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(),
                export_interval_millis=settings.telemetry_metric_interval_ms,
            )
        )
    provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(provider)
    telemetry.meter_provider = provider


def _configure_logs(resource: Resource, telemetry: Telemetry) -> None:
    """Bridge the stdlib logger onto OTLP, leaving the stderr stream as it is."""
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(provider)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        handler = StructuredLoggingHandler(logger_provider=provider)

    # Attached to the namespace logger, so it sees exactly the records our own
    # code emits and inherits the level configured for them.
    logging.getLogger(LOGGER_NAMESPACE).addHandler(handler)

    telemetry.logger_provider = provider
    telemetry.log_handler = handler
