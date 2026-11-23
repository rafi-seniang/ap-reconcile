"""Unified LiteLLM multimodal page extractor with exponential backoff, JSON repair, and optional disk cache."""

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, get_args

import litellm
from litellm.exceptions import (
    APIConnectionError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from pydantic import BaseModel, ValidationError

from app.llm.pdf_splitter import PdfPagePayload
from app.prompts.extraction_prompt import (
    SYSTEM_EXTRACTION_PROMPT,
    build_math_retry_user_prompt,
    build_page_extraction_user_prompt,
)
from app.schemas.invoice import PrintedPage
from config.settings import Settings, infer_provider_name

logger = logging.getLogger(__name__)

_TRANSIENT_LITELLM_ERRORS = (
    RateLimitError,
    ServiceUnavailableError,
    InternalServerError,
    APIConnectionError,
    Timeout,
)


class UpstreamUnavailable(RuntimeError):
    """Raised when the configured LLM provider fails or returns invalid structured output."""


@dataclass(frozen=True)
class TokenCount:
    """Token counts returned by a single page extraction call."""

    input_tokens: int = 0
    output_tokens: int = 0
    from_cache: bool = False


class PageExtractor(Protocol):
    """Minimal page extraction contract so offline tests can inject a deterministic fake."""

    provider_name: str
    model_name: str

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]: ...

    async def close(self) -> None: ...


def extract_json_object_text(raw_text: str) -> str:
    """Strip markdown fences or surrounding prose to isolate the outer JSON object."""
    start_idx, end_idx = raw_text.find("{"), raw_text.rfind("}")
    return raw_text[start_idx : end_idx + 1] if start_idx != -1 and end_idx > start_idx else raw_text


def build_schema_outline(schema_class: type[BaseModel]) -> dict[str, Any]:
    """Build a compact field-description outline for vision models returning JSON objects."""
    outline: dict[str, Any] = {}
    for field_name, field_info in schema_class.model_fields.items():
        inner_types = get_args(field_info.annotation)
        nested_model = next(
            (kind for kind in inner_types if isinstance(kind, type) and issubclass(kind, BaseModel)),
            None,
        )
        outline[field_name] = (
            [build_schema_outline(nested_model)] if nested_model else (field_info.description or field_name)
        )
    return outline


class LiteLLMPageExtractor:
    """Provider-agnostic multimodal extractor using LiteLLM (supports Gemini, DeepSeek, OpenAI, Claude)."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        api_base: str | None = None,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
        max_retries: int = 3,
        is_retry_role: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("LiteLLMPageExtractor requires a non-empty API key")
        # Allow LiteLLM to drop provider-unsupported knobs (such as temperature on Gemini 3.5 Flash-Lite).
        litellm.drop_params = True
        litellm.suppress_debug_info = True

        self.model_name = model_name
        self.provider_name = infer_provider_name(model_name, api_base)
        self._api_key = api_key
        self._api_base = api_base
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._is_retry_role = is_retry_role
        self._schema_outline_json = json.dumps(build_schema_outline(PrintedPage), ensure_ascii=False)

    def _build_completion_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        is_math_retry: bool,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "api_key": self._api_key,
            "temperature": 0.0,
            "max_tokens": self._max_output_tokens,
            "timeout": self._timeout_seconds,
            "num_retries": 0,
            "response_format": {"type": "json_object"},
        }
        if self._api_base:
            request_kwargs["api_base"] = self._api_base
        if self.provider_name == "deepseek":
            request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        elif self.provider_name == "gemini":
            request_kwargs["reasoning_effort"] = "medium" if (is_math_retry or self._is_retry_role) else "low"
        return request_kwargs

    async def _complete_with_backoff(
        self,
        messages: list[dict[str, Any]],
        *,
        page_number: int,
        is_math_retry: bool,
    ) -> tuple[str, TokenCount]:
        request_kwargs = self._build_completion_kwargs(messages, is_math_retry=is_math_retry)
        total_attempts = self._max_retries + 1
        last_error: Exception | None = None

        for attempt in range(total_attempts):
            try:
                response: Any = await litellm.acompletion(**request_kwargs)
                usage = getattr(response, "usage", None)
                tokens = TokenCount(
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                )
                content = str(response.choices[0].message.content or "{}")
                return content, tokens
            except _TRANSIENT_LITELLM_ERRORS as exc:
                last_error = exc
                if attempt == total_attempts - 1:
                    break
                delay_seconds = min(2.0 * (2**attempt), 20.0)
                logger.warning(
                    "LiteLLM transient error on page %s (%s, attempt %s/%s); retrying in %.1fs",
                    page_number,
                    type(exc).__name__,
                    attempt + 1,
                    total_attempts,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)
            except Exception as exc:
                last_error = exc
                break

        raise UpstreamUnavailable(
            f"LLM extraction failed for page {page_number} with model {self.model_name}: {last_error}"
        ) from last_error

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        user_prompt = (
            build_math_retry_user_prompt(page.page_number, retry_feedback)
            if retry_feedback
            else build_page_extraction_user_prompt(page.page_number, total_pages)
        )
        encoded_png_url = "data:image/png;base64," + base64.b64encode(page.png_bytes).decode("ascii")
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                    f"Return exactly one JSON object matching this shape:\n{self._schema_outline_json}"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": encoded_png_url}},
                ],
            },
        ]

        first_text, first_tokens = await self._complete_with_backoff(
            messages,
            page_number=page.page_number,
            is_math_retry=bool(retry_feedback),
        )
        try:
            return PrintedPage.model_validate_json(extract_json_object_text(first_text)), first_tokens
        except ValidationError as exc:
            logger.warning(
                "Model output failed schema validation on page %s, requesting one JSON repair: %s",
                page.page_number,
                exc.errors()[0],
            )
            messages.extend(
                [
                    {"role": "assistant", "content": first_text},
                    {
                        "role": "user",
                        "content": f"That JSON failed validation: {exc}. Return only the corrected JSON object.",
                    },
                ]
            )

        repaired_text, repair_tokens = await self._complete_with_backoff(
            messages,
            page_number=page.page_number,
            is_math_retry=True,
        )
        combined_tokens = TokenCount(
            input_tokens=first_tokens.input_tokens + repair_tokens.input_tokens,
            output_tokens=first_tokens.output_tokens + repair_tokens.output_tokens,
        )
        try:
            return PrintedPage.model_validate_json(extract_json_object_text(repaired_text)), combined_tokens
        except ValidationError as exc:
            raise UpstreamUnavailable(
                f"Model {self.model_name} did not return valid JSON for page {page.page_number}"
            ) from exc

    async def close(self) -> None:
        return None


class DiskCachedExtractor:
    """Caches page extraction JSON on disk keyed by rendered page PNG bytes, provider, model, and prompt."""

    def __init__(self, inner_extractor: PageExtractor, cache_dir: Path | str) -> None:
        self._inner = inner_extractor
        self.provider_name = inner_extractor.provider_name
        self.model_name = inner_extractor.model_name
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _build_cache_path(self, page: PdfPagePayload, retry_feedback: list[str] | None) -> Path:
        hasher = hashlib.sha256()
        hasher.update(self.provider_name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(self.model_name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(SYSTEM_EXTRACTION_PROMPT.encode("utf-8"))
        hasher.update(b"\0")
        # Hash deterministic rendered PNG bytes because PyMuPDF sub-PDF trailers vary across runs.
        hasher.update(page.png_bytes)
        if retry_feedback:
            hasher.update(b"\0retry\0")
            hasher.update("|".join(retry_feedback).encode("utf-8"))
        return self._cache_dir / f"{hasher.hexdigest()}.json"

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        cache_path = self._build_cache_path(page, retry_feedback)
        if cache_path.is_file():
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return PrintedPage.model_validate(cached_payload["printed"]), TokenCount(0, 0, from_cache=True)

        printed, tokens = await self._inner.extract_page(page, total_pages, retry_feedback=retry_feedback)
        record = {
            "provider": self.provider_name,
            "model": self.model_name,
            "page_number": page.page_number,
            "recorded_input_tokens": tokens.input_tokens,
            "recorded_output_tokens": tokens.output_tokens,
            "printed": printed.model_dump(mode="json"),
        }
        cache_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return printed, tokens

    async def close(self) -> None:
        await self._inner.close()


def estimate_extraction_cost_usd(
    settings: Settings,
    *,
    primary_input_tokens: int,
    primary_output_tokens: int,
    retry_input_tokens: int,
    retry_output_tokens: int,
) -> float | None:
    """Compute transparent USD token cost when model prices are set in settings; otherwise None."""
    if settings.llm_input_price_per_million_usd is None or settings.llm_output_price_per_million_usd is None:
        return None

    total_cost_usd = (
        primary_input_tokens * settings.llm_input_price_per_million_usd
        + primary_output_tokens * settings.llm_output_price_per_million_usd
    ) / 1_000_000.0

    if retry_input_tokens or retry_output_tokens:
        retry_in_price = (
            settings.llm_retry_input_price_per_million_usd
            if settings.llm_retry_input_price_per_million_usd is not None
            else settings.llm_input_price_per_million_usd
        )
        retry_out_price = (
            settings.llm_retry_output_price_per_million_usd
            if settings.llm_retry_output_price_per_million_usd is not None
            else settings.llm_output_price_per_million_usd
        )
        total_cost_usd += (
            retry_input_tokens * retry_in_price + retry_output_tokens * retry_out_price
        ) / 1_000_000.0

    return round(total_cost_usd, 6)
