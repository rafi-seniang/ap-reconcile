"""Application settings loaded from environment variables and .env."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_key_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines from an optional local secrets file."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            values[key.strip()] = cleaned
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider and model routing
    llm_provider: Literal["gemini", "openai_compat"] = Field(
        default="gemini",
        description="gemini sends native 1-page PDFs; openai_compat sends rendered page PNGs",
    )
    llm_model: str = Field(default="gemini-3.5-flash-lite", description="Primary page extraction model")
    llm_retry_model: str | None = Field(
        default="gemini-3.5-flash-lite",
        description="Model for 1x targeted retry when a document fails math or parse checks",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="Base URL for openai_compat provider (e.g. https://api.deepseek.com)",
    )
    llm_api_key: SecretStr | None = Field(default=None, description="Provider API key")
    secrets_env_path: str | None = Field(
        default=None,
        description="Optional path to a local secrets file checked when LLM_API_KEY is unset",
    )

    # Concurrency, timeouts, and image rendering
    llm_concurrency: int = Field(default=3, ge=1, le=20, description="Max concurrent page extraction calls")
    llm_timeout_seconds: float = Field(default=90.0, gt=0, description="Per-request timeout in seconds")
    llm_max_output_tokens: int = Field(default=4096, ge=512, description="Max output tokens per page call")
    pdf_render_dpi: int = Field(
        default=200, ge=100, le=400, description="DPI for PNG preview and vision fallback"
    )

    # Optional token prices (USD per 1M tokens) for transparent cost tracking
    llm_input_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_output_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_retry_input_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_retry_output_price_per_million_usd: float | None = Field(default=None, ge=0)

    # Business rules (R5 and R7 policy knob for wrong DPP base)
    tolerance_idr: int = Field(default=10, ge=0, description="R5 rounding tolerance in whole rupiah")
    tax_base_error_as_needs_review: bool = Field(
        default=True,
        description="Flag PDF invoices violating R3 11/12 DPP base as NEEDS_REVIEW (True) or AMOUNT_MISMATCH (False)",
    )

    # Local extraction cache directory (empty disables disk caching)
    extraction_cache_dir: str | None = Field(
        default=None, description="Directory for cached page extractions"
    )

    def resolve_api_keys(self) -> list[str]:
        """Return one or more API keys for the configured provider, failing loudly if none exist."""
        if self.llm_api_key is not None and self.llm_api_key.get_secret_value().strip():
            raw = self.llm_api_key.get_secret_value()
            keys = [item.strip() for item in raw.split(",") if item.strip()]
            if keys:
                return keys

        if self.secrets_env_path:
            secret_map = _read_key_file(Path(self.secrets_env_path))
            if self.llm_provider == "gemini":
                gemini_keys = [
                    secret_map[name]
                    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
                    if name in secret_map
                ]
                if gemini_keys:
                    return gemini_keys
            else:
                if (
                    self.llm_base_url
                    and "deepseek" in self.llm_base_url.lower()
                    and "DEEPSEEK_API_KEY" in secret_map
                ):
                    return [secret_map["DEEPSEEK_API_KEY"]]
                for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
                    if name in secret_map:
                        return [secret_map[name]]

        raise ValueError(f"Missing API key for provider '{self.llm_provider}'. Set LLM_API_KEY in .env.")
