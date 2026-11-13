"""Reset OpenTelemetry's process-wide providers between tests.

Each global provider is guarded by a one-shot latch: a second
``set_tracer_provider`` logs a warning and keeps the first one. That is right for
a running service and wrong for a test suite, where one module installs real
OTLP providers and another needs in-memory ones — whichever test ran first would
decide what every later test sees.

The upstream helper for this lives in ``opentelemetry-test-utils``, which is not
a dependency here, so the few globals it touches are reset directly.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry._logs import _internal as logs_internal
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.util._once import Once


def reset_otel_globals() -> None:
    """Forget the installed tracer, meter and logger providers."""
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None

    metrics_internal._METER_PROVIDER_SET_ONCE = Once()
    metrics_internal._METER_PROVIDER = None

    logs_internal._LOGGER_PROVIDER_SET_ONCE = Once()
    logs_internal._LOGGER_PROVIDER = None
