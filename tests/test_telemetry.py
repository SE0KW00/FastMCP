"""Telemetry wiring: enablement, resource, log bridge and shutdown."""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk._logs import LoggingHandler

from es_search_mcp import telemetry as telemetry_module
from es_search_mcp.config import Settings
from es_search_mcp.logging import LOGGER_NAMESPACE
from es_search_mcp.telemetry import (
    StructuredLoggingHandler,
    Telemetry,
    _flatten,
    build_resource,
    collector_is_configured,
    configure_telemetry,
    environment_declared_keys,
    telemetry_is_enabled,
)
from tests.otel_globals import reset_otel_globals

#: A port nothing listens on, so an export attempt is refused at once instead
#: of spending the exporter's retry budget on DNS or a connect timeout. These
#: cases are about the wiring, not about reaching a collector.
ENDPOINT = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _clean_otel_env(monkeypatch):
    """Start every case with no collector configured and nothing installed."""
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telemetry_module, "_active", None)
    # These cases install real providers; reset the process-wide latches so a
    # later module can install its own.
    reset_otel_globals()
    yield
    monkeypatch.setattr(telemetry_module, "_active", None)
    reset_otel_globals()


# --------------------------------------------------------------------------- #
# Enablement
# --------------------------------------------------------------------------- #
def test_without_a_collector_telemetry_stays_off(settings):
    assert collector_is_configured() is False
    assert telemetry_is_enabled(settings) is False


@pytest.mark.parametrize(
    "variable",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ],
)
def test_any_endpoint_variable_switches_it_on(settings, monkeypatch, variable):
    monkeypatch.setenv(variable, ENDPOINT)
    assert telemetry_is_enabled(settings) is True


def test_a_blank_endpoint_does_not_count(settings, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    assert telemetry_is_enabled(settings) is False


def test_the_setting_overrides_the_endpoint_either_way(settings, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    assert telemetry_is_enabled(settings.model_copy(update={"telemetry_enabled": False})) is False
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert telemetry_is_enabled(settings.model_copy(update={"telemetry_enabled": True})) is True


def test_a_disabled_handle_needs_no_branch_from_callers(settings):
    disabled = configure_telemetry(settings)
    assert disabled.enabled is False
    assert disabled.tracer_provider is None
    assert disabled.log_handler is None
    disabled.shutdown()  # must not raise


# --------------------------------------------------------------------------- #
# Resource
# --------------------------------------------------------------------------- #
def test_resource_reports_the_service_name_and_version(settings):
    attributes = build_resource(settings).attributes
    assert attributes["service.name"] == "es-search-mcp"
    assert attributes["service.version"]


def test_environment_is_reported_under_both_semconv_spellings(settings):
    configured = settings.model_copy(update={"telemetry_environment": "staging"})
    attributes = build_resource(configured).attributes
    assert attributes["deployment.environment.name"] == "staging"
    assert attributes["deployment.environment"] == "staging"


def test_environment_is_omitted_when_unset(settings):
    assert "deployment.environment" not in build_resource(settings).attributes


def test_the_standard_env_var_wins_over_the_setting(settings, monkeypatch):
    """The deployment gets the last word over the application default."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "search-mcp-canary")
    assert build_resource(settings).attributes["service.name"] == "search-mcp-canary"


def test_resource_attributes_from_the_environment_also_win(settings, monkeypatch):
    configured = settings.model_copy(update={"telemetry_environment": "staging"})
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=prod")

    attributes = build_resource(configured).attributes
    assert attributes["deployment.environment.name"] == "prod"
    # The spelling the environment did not claim keeps the application default.
    assert attributes["deployment.environment"] == "staging"


def test_declared_keys_are_read_from_both_standard_variables(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "svc")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "a=1, b=2 ,malformed,=3")
    assert environment_declared_keys() == {"service.name", "a", "b"}


# --------------------------------------------------------------------------- #
# Attribute flattening for the log bridge
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("faq", {"f": "faq"}, id="str"),
        pytest.param(3, {"f": 3}, id="int"),
        pytest.param(1.5, {"f": 1.5}, id="float"),
        pytest.param(True, {"f": True}, id="bool"),
        pytest.param(None, {"f": None}, id="none"),
        pytest.param(["a", "b"], {"f": ["a", "b"]}, id="str-list"),
    ],
)
def test_primitives_and_string_lists_pass_through(value, expected):
    into: dict[str, object] = {}
    _flatten("f", value, into)
    assert into == expected


def test_nested_dicts_become_dotted_keys():
    into: dict[str, object] = {}
    _flatten("details", {"method": "GET", "status_code": 404}, into)
    assert into == {"details.method": "GET", "details.status_code": 404}


def test_deeply_nested_dicts_are_flattened_all_the_way():
    into: dict[str, object] = {}
    _flatten("a", {"b": {"c": 1}}, into)
    assert into == {"a.b.c": 1}


def test_values_otlp_cannot_carry_are_stringified_not_dropped():
    into: dict[str, object] = {}
    _flatten("f", [1, "mixed"], into)
    assert into["f"] == "[1, 'mixed']"


def test_the_handler_spreads_our_fields_into_attributes():
    record = logging.LogRecord(
        "es_search_mcp.test", logging.INFO, __file__, 1, "tool.call.failed", None, None
    )
    record.fields = {"code": "BACKEND_TIMEOUT", "details": {"path": "/indices"}}

    attributes = StructuredLoggingHandler._get_attributes(record)

    # The nested mapping is gone; each field is queryable on its own.
    assert "fields" not in attributes
    assert attributes["code"] == "BACKEND_TIMEOUT"
    assert attributes["details.path"] == "/indices"


def test_the_handler_is_a_real_otlp_log_handler():
    """It has to stay a LoggingHandler: that is what bridges stdlib to OTLP."""
    assert issubclass(StructuredLoggingHandler, LoggingHandler)


# --------------------------------------------------------------------------- #
# Configuration and shutdown
# --------------------------------------------------------------------------- #
@pytest.fixture
def enabled_settings(settings, monkeypatch) -> Settings:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    return settings.model_copy(update={"telemetry_enabled": True, "telemetry_environment": "test"})


@pytest.fixture
def configured(enabled_settings) -> Telemetry:
    telemetry = configure_telemetry(enabled_settings)
    yield telemetry
    telemetry.shutdown()


def test_all_three_signals_are_configured(configured):
    assert configured.enabled is True
    assert configured.tracer_provider is not None
    assert configured.meter_provider is not None
    assert configured.logger_provider is not None


def test_the_log_bridge_is_attached_to_the_namespace_logger(configured):
    handlers = logging.getLogger(LOGGER_NAMESPACE).handlers
    assert configured.log_handler in handlers


def test_log_export_can_be_turned_off(enabled_settings):
    telemetry = configure_telemetry(
        enabled_settings.model_copy(update={"telemetry_export_logs": False})
    )
    try:
        assert telemetry.logger_provider is None
        assert telemetry.log_handler is None
        # Traces and metrics are unaffected.
        assert telemetry.tracer_provider is not None
    finally:
        telemetry.shutdown()


def test_configuring_twice_returns_the_same_handle(configured, enabled_settings):
    """Providers are process-wide; a second install would export everything twice."""
    assert configure_telemetry(enabled_settings) is configured
    assert (
        len(
            [
                handler
                for handler in logging.getLogger(LOGGER_NAMESPACE).handlers
                if isinstance(handler, LoggingHandler)
            ]
        )
        == 1
    )


def test_shutdown_detaches_the_log_handler(enabled_settings):
    telemetry = configure_telemetry(enabled_settings)
    handler = telemetry.log_handler
    telemetry.shutdown()

    assert telemetry.enabled is False
    assert telemetry.log_handler is None
    assert handler not in logging.getLogger(LOGGER_NAMESPACE).handlers


def test_shutdown_is_safe_to_call_twice(enabled_settings):
    telemetry = configure_telemetry(enabled_settings)
    telemetry.shutdown()
    telemetry.shutdown()


def test_shutdown_clears_the_active_handle_so_it_can_be_reconfigured(enabled_settings):
    first = configure_telemetry(enabled_settings)
    first.shutdown()
    second = configure_telemetry(enabled_settings)
    try:
        assert second is not first
        assert second.enabled is True
    finally:
        second.shutdown()
