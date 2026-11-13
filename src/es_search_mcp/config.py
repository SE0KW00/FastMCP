"""Runtime configuration for the Elasticsearch search MCP server.

All settings are read from environment variables (or a local ``.env`` file)
using the ``ES_MCP_`` prefix, e.g. ``ES_MCP_BACKEND_BASE_URL``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from es_search_mcp.models import RetrieveMethod

LogFormat = Literal["json", "text"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Transport = Literal["stdio", "http", "sse"]


class Settings(BaseSettings):
    """Server settings.

    The defaults are safe for local development; every value can be overridden
    through the environment so that the same image runs in every stage.
    """

    model_config = SettingsConfigDict(
        env_prefix="ES_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Backend API -----------------------------------------------------
    backend_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the backend API that talks to Elasticsearch.",
    )
    backend_indices_path: str = Field(
        default="/indices",
        description="Path of the endpoint returning index names and descriptions.",
    )
    backend_retrieve_path_template: str = Field(
        default="/retrieve-{method}",
        description=(
            "Path template of the retrieval endpoints. `{method}` is replaced by the "
            "retrieval strategy, giving /retrieve-bm25, /retrieve-knn, /retrieve-cc "
            "and /retrieve-rrf."
        ),
    )

    # --- Caller credentials ----------------------------------------------
    # The two keys are supplied per request by the MCP client and forwarded to
    # the backend unchanged; this server never stores or defaults them.
    auth_header: str = Field(
        default="authorization",
        description="Incoming header carrying the first key, forwarded under the same name.",
    )
    api_key_header: str = Field(
        default="x-api-key",
        description="Incoming header carrying the second key, forwarded under the same name.",
    )

    # --- HTTP behaviour --------------------------------------------------
    request_timeout: float = Field(
        default=30.0, gt=0, description="Per-request timeout in seconds."
    )
    max_retries: int = Field(
        default=2, ge=0, le=5, description="Retry attempts for transient backend failures."
    )
    retry_backoff_base: float = Field(
        default=0.5, gt=0, description="Base delay in seconds for exponential backoff."
    )

    # --- Search defaults -------------------------------------------------
    default_top_k: int = Field(default=5, ge=1, description="Default number of documents.")
    max_top_k: int = Field(default=50, ge=1, description="Upper bound accepted for top_k.")
    default_retrieve_method: RetrieveMethod = Field(
        default=RetrieveMethod.RRF,
        description="Retrieval strategy used when the caller does not name one.",
    )
    default_permission_groups: list[str] = Field(
        default=["rag-public"],
        description=(
            "Permission groups sent to the backend when the caller does not supply "
            "any. Set it to the group that carries no special access."
        ),
    )

    # --- OpenTelemetry ---------------------------------------------------
    # Endpoint, headers, protocol and sampling are read from the standard
    # OTEL_* environment variables by the SDK itself, so they are deliberately
    # not duplicated here. These settings cover only what the SDK has no
    # convention for.
    telemetry_enabled: bool | None = Field(
        default=None,
        description=(
            "Turn OpenTelemetry export on or off. Left unset, it switches on "
            "whenever OTEL_EXPORTER_OTLP_ENDPOINT (or its traces/metrics/logs "
            "variant) is set, so an unconfigured deployment stays silent."
        ),
    )
    telemetry_service_name: str = Field(
        default="es-search-mcp",
        description="service.name reported to APM. OTEL_SERVICE_NAME wins when set.",
    )
    telemetry_environment: str | None = Field(
        default=None,
        description="deployment.environment.name reported to APM, e.g. 'prod' or 'staging'.",
    )
    telemetry_metric_interval_ms: int = Field(
        default=60_000,
        ge=1_000,
        description="How often metrics are pushed to the collector, in milliseconds.",
    )
    telemetry_export_logs: bool = Field(
        default=True,
        description=(
            "Also ship log records over OTLP. Turn it off when logs are already "
            "collected from stderr by a log shipper, to avoid indexing them twice."
        ),
    )
    telemetry_console_export: bool = Field(
        default=False,
        description="Print spans and metrics to stderr as well. For debugging only.",
    )

    # --- Observability ---------------------------------------------------
    log_level: LogLevel = Field(default="INFO", description="Root log level for the server.")
    log_format: LogFormat = Field(
        default="json", description="'json' for machine-readable logs, 'text' for humans."
    )
    log_tool_arguments: bool = Field(
        default=True,
        description=(
            "Include the caller's tool arguments in the `tool.call.start` log "
            "event. They are the fastest way to reproduce a bad call, but they "
            "contain the end user's search text — turn this off where queries "
            "must not be retained in the log store or in APM."
        ),
    )
    mask_error_details: bool = Field(
        default=True,
        description="Hide internal details of unexpected errors from MCP clients.",
    )

    # --- Transport -------------------------------------------------------
    transport: Transport = Field(
        default="http",
        description=(
            "MCP transport to serve on. Caller credentials arrive as HTTP headers, so "
            "`stdio` cannot supply them and tool calls will fail with MISSING_CREDENTIALS."
        ),
    )
    host: str = Field(default="127.0.0.1", description="Bind host for HTTP transports.")
    port: int = Field(default=8080, ge=1, le=65535, description="Bind port for HTTP transports.")

    @field_validator("backend_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("backend_indices_path", "backend_retrieve_path_template")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @field_validator("backend_retrieve_path_template")
    @classmethod
    def _require_method_placeholder(cls, value: str) -> str:
        if "{method}" not in value:
            raise ValueError("backend_retrieve_path_template must contain '{method}'")
        return value

    @field_validator("auth_header", "api_key_header")
    @classmethod
    def _normalise_header_name(cls, value: str) -> str:
        normalised = value.strip().lower()
        if not normalised:
            raise ValueError("header names must not be empty")
        return normalised

    def retrieve_path(self, method: RetrieveMethod) -> str:
        """Return the backend path serving ``method``."""
        return self.backend_retrieve_path_template.format(method=method.value)

    def model_post_init(self, __context: object) -> None:
        if self.default_top_k > self.max_top_k:
            raise ValueError("default_top_k must not be greater than max_top_k")


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment."""
    return Settings()
