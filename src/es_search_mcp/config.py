"""Runtime configuration for the Elasticsearch search MCP server.

All settings are read from environment variables (or a local ``.env`` file)
using the ``ES_MCP_`` prefix, e.g. ``ES_MCP_BACKEND_BASE_URL``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    backend_api_key: SecretStr | None = Field(
        default=None,
        description="Optional bearer token sent to the backend API.",
    )
    backend_indices_path: str = Field(
        default="/indices",
        description="Path of the endpoint returning index names and descriptions.",
    )
    backend_retrieve_path: str = Field(
        default="/retrieve",
        description="Path of the endpoint returning documents for a query.",
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

    # --- Observability ---------------------------------------------------
    log_level: LogLevel = Field(default="INFO", description="Root log level for the server.")
    log_format: LogFormat = Field(
        default="json", description="'json' for machine-readable logs, 'text' for humans."
    )
    mask_error_details: bool = Field(
        default=True,
        description="Hide internal details of unexpected errors from MCP clients.",
    )

    # --- Transport -------------------------------------------------------
    transport: Transport = Field(default="stdio", description="MCP transport to serve on.")
    host: str = Field(default="127.0.0.1", description="Bind host for HTTP transports.")
    port: int = Field(default=8080, ge=1, le=65535, description="Bind port for HTTP transports.")

    @field_validator("backend_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("backend_indices_path", "backend_retrieve_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    def model_post_init(self, __context: object) -> None:
        if self.default_top_k > self.max_top_k:
            raise ValueError("default_top_k must not be greater than max_top_k")


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment."""
    return Settings()
