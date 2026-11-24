"""Application settings loaded from environment variables and .env."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


def read_local_secrets_file(secrets_path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines from an optional local secrets file."""
    if not secrets_path.is_file():
        return {}
    parsed_values: dict[str, str] = {}
    for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key_name, raw_value = stripped_line.split("=", 1)
        cleaned_value = raw_value.strip().strip('"').strip("'")
        if cleaned_value:
            parsed_values[key_name.strip()] = cleaned_value
    return parsed_values


def infer_provider_name(model_name: str, api_base: str | None = None) -> str:
    """Derive a clean provider label from a LiteLLM model string and optional base URL."""
    lowered_model = model_name.lower()
    if "deepseek" in lowered_model or (api_base and "deepseek" in api_base.lower()):
        return "deepseek"
    if "/" in model_name:
        return model_name.split("/", 1)[0].lower()
    if lowered_model.startswith("gemini"):
        return "gemini"
    return "openai"


def _secret_to_str(secret: SecretStr | None) -> str | None:
    if secret is None:
        return None
    cleaned = secret.get_secret_value().strip()
    return cleaned or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Primary and optional retry / automatic failover LiteLLM models
    llm_model: str = Field(
        default="gemini/gemini-3.5-flash-lite",
        description="Primary LiteLLM vision model for page extraction",
    )
    llm_retry_model: str | None = Field(
        default=None,
        description="Optional override model for 1x math-gate retry (defaults to llm_model when blank)",
    )
    llm_fallback_model: str | None = Field(
        default=None,
        description="Optional LiteLLM failover model invoked immediately when the primary model fails",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="Optional custom API base URL for primary model",
    )
    llm_fallback_base_url: str | None = Field(
        default=None,
        description="Optional custom API base URL for fallback model",
    )

    # Standard provider API keys (matching LiteLLM conventions) plus generic overrides
    gemini_api_key: SecretStr | None = Field(default=None)
    deepseek_api_key: SecretStr | None = Field(default=None)
    openai_api_key: SecretStr | None = Field(default=None)
    anthropic_api_key: SecretStr | None = Field(default=None)
    llm_api_key: SecretStr | None = Field(default=None, description="Generic primary API key override")
    llm_fallback_api_key: SecretStr | None = Field(
        default=None, description="Generic fallback API key override"
    )
    secrets_env_path: str | None = Field(
        default=None,
        description="Optional path to a local secrets file checked when env keys are unset",
    )

    # Concurrency, sampling, retries, timeouts, and image rendering
    llm_temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Sampling temperature (auto-dropped on models that ignore it)"
    )
    llm_reasoning_effort: ReasoningEffort = Field(
        default="none",
        description="LiteLLM reasoning_effort ('none' disables CoT on DeepSeek and uses minimal/low on Gemini 3)",
    )
    llm_max_retries: int = Field(
        default=3, ge=0, le=10, description="Max backoff retries on primary model when no fallback is active"
    )
    llm_concurrency: int = Field(default=3, ge=1, le=20, description="Max concurrent page extraction calls")
    llm_timeout_seconds: float = Field(default=90.0, gt=0, description="Per-request timeout in seconds")
    llm_max_output_tokens: int = Field(default=4096, ge=512, description="Max output tokens per page call")
    pdf_render_dpi: int = Field(
        default=200, ge=100, le=400, description="DPI for page PNG rendering sent to vision models and UI"
    )

    # Optional token prices (USD per 1M tokens) for transparent cost tracking
    llm_input_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_output_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_retry_input_price_per_million_usd: float | None = Field(default=None, ge=0)
    llm_retry_output_price_per_million_usd: float | None = Field(default=None, ge=0)

    # Business rules (R5 tolerance and R3/R7 tax-base policy)
    tolerance_idr: int = Field(default=10, ge=0, description="R5 rounding tolerance in whole rupiah")
    tax_base_error_as_needs_review: bool = Field(
        default=True,
        description="Route PDF invoices violating R3 11/12 DPP base to NEEDS_REVIEW (True) or AMOUNT_MISMATCH (False)",
    )

    # Local disk extraction cache (empty disables caching)
    extraction_cache_dir: str | None = Field(
        default=None, description="Directory for content-addressed page extraction cache"
    )

    # Lightweight HTTP API guards (in-memory single-process rate limit and upload size caps)
    rate_limit_requests_per_window: int = Field(
        default=10, ge=1, description="Max reconciliation POST requests per IP within the rate-limit window"
    )
    rate_limit_window_seconds: int = Field(
        default=60, ge=1, description="Sliding window duration in seconds for per-IP rate limiting"
    )
    max_pdf_upload_mb: int = Field(default=25, ge=1, description="Maximum allowed PDF upload size in MB")
    max_erp_upload_mb: int = Field(default=10, ge=1, description="Maximum allowed ERP workbook size in MB")
    max_stored_runs: int = Field(
        default=10, ge=1, le=100, description="Maximum recent reconciliation runs kept in web UI memory"
    )

    def _lookup_provider_key(self, provider: str) -> str | None:
        direct_by_provider: dict[str, SecretStr | None] = {
            "gemini": self.gemini_api_key,
            "deepseek": self.deepseek_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }
        direct_value = _secret_to_str(direct_by_provider.get(provider))
        if direct_value:
            return direct_value

        if self.secrets_env_path:
            secret_map = read_local_secrets_file(Path(self.secrets_env_path))
            env_names_by_provider: dict[str, tuple[str, ...]] = {
                "gemini": ("GEMINI_API_KEY",),
                "deepseek": ("DEEPSEEK_API_KEY",),
                "anthropic": ("ANTHROPIC_API_KEY",),
                "openai": ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
            }
            for env_name in env_names_by_provider.get(provider, ("OPENAI_API_KEY", "GEMINI_API_KEY")):
                if env_name in secret_map:
                    return secret_map[env_name]
        return None

    def resolve_api_key_for_model(
        self,
        model_name: str,
        api_base: str | None = None,
        *,
        explicit_override: SecretStr | None = None,
    ) -> str | None:
        """Resolve an API key for a given model from explicit override, provider env var, or secrets file."""
        override_value = _secret_to_str(explicit_override)
        if override_value:
            return override_value
        provider = infer_provider_name(model_name, api_base)
        return self._lookup_provider_key(provider)

    def resolve_primary_api_key(self) -> str:
        """Return the primary model API key, failing loudly if none is configured."""
        resolved = self.resolve_api_key_for_model(
            self.llm_model, self.llm_base_url, explicit_override=self.llm_api_key
        )
        if resolved:
            return resolved
        provider = infer_provider_name(self.llm_model, self.llm_base_url)
        raise ValueError(
            f"Missing API key for provider '{provider}'. Set {provider.upper()}_API_KEY or LLM_API_KEY in .env."
        )

    def resolve_fallback_api_key(self) -> str | None:
        """Return the fallback model API key when llm_fallback_model is set, or None if unavailable."""
        if not self.llm_fallback_model or not self.llm_fallback_model.strip():
            return None
        return self.resolve_api_key_for_model(
            self.llm_fallback_model,
            self.llm_fallback_base_url,
            explicit_override=self.llm_fallback_api_key or self.llm_api_key,
        )
