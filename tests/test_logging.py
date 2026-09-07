"""Structured logging: context binding, redaction and formatting."""

from __future__ import annotations

import json
import logging

from es_search_mcp.logging import (
    JsonFormatter,
    TextFormatter,
    bind_call_context,
    configure_logging,
    current_call_context,
    get_logger,
    redact,
)


def _record(**fields):
    record = logging.LogRecord(
        "es_search_mcp.test", logging.INFO, __file__, 1, "tool.call.start", None, None
    )
    record.fields = fields
    return record


def test_json_formatter_emits_one_object_per_line():
    payload = json.loads(JsonFormatter().format(_record(index="faq", duration_ms=12.5)))
    assert payload["event"] == "tool.call.start"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "es_search_mcp.test"
    assert payload["index"] == "faq"
    assert payload["duration_ms"] == 12.5
    assert "timestamp" in payload


def test_text_formatter_appends_fields():
    assert "index=faq" in TextFormatter().format(_record(index="faq"))


def test_text_formatter_does_not_echo_formatter_internals():
    """`Formatter.format` adds `message` and `asctime` to the record itself."""
    rendered = TextFormatter().format(_record(index="faq"))
    assert "message=" not in rendered
    assert "asctime=" not in rendered


def test_redaction_covers_nested_structures():
    cleaned = redact({"api_key": "x", "nested": [{"token": "y", "keep": "z"}]})
    assert cleaned == {"api_key": "***", "nested": [{"token": "***", "keep": "z"}]}


def test_json_formatter_redacts_sensitive_fields():
    payload = json.loads(JsonFormatter().format(_record(authorization="Bearer abc")))
    assert payload["authorization"] == "***"


def test_call_context_is_bound_and_restored():
    assert current_call_context() == {}
    with bind_call_context(request_id="r1", tool_name="list_indices") as request_id:
        assert request_id == "r1"
        assert current_call_context() == {"request_id": "r1", "tool_name": "list_indices"}
    assert current_call_context() == {}


def test_call_context_generates_a_request_id_when_absent():
    with bind_call_context(tool_name="t") as request_id:
        assert request_id and len(request_id) == 16


def test_bound_context_lands_on_emitted_records(capsys):
    configure_logging(level="INFO", log_format="json")
    with bind_call_context(request_id="r9", tool_name="retrieve_documents"):
        get_logger(__name__).info("tool.call.start", fields={"index": "faq"})

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["request_id"] == "r9"
    assert payload["tool_name"] == "retrieve_documents"
    assert payload["index"] == "faq"


def test_logs_go_to_stderr_never_stdout(capsys):
    """stdout carries the MCP protocol on the stdio transport."""
    configure_logging(level="INFO", log_format="text")
    get_logger(__name__).info("server.starting")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "server.starting" in captured.err


def test_configure_logging_is_idempotent():
    configure_logging()
    configure_logging()
    assert len(logging.getLogger("es_search_mcp").handlers) == 1
