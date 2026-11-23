"""Multimodal page extractors (Gemini native PDF and OpenAI-compatible vision) with optional disk cache."""

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, get_args

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openai import APIError as OpenAIAPIError
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.extraction.prompts import (
    SYSTEM_EXTRACTION_PROMPT,
    build_page_user_prompt,
    build_retry_user_prompt,
)
from app.extraction.schemas import PrintedPage
from app.extraction.splitter import PdfPagePayload
from config.settings import Settings

logger = logging.getLogger(__name__)


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


def json_object_text(text: str) -> str:
    """Strip markdown fences or surrounding prose to isolate the outer JSON object."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def schema_outline(schema: type[BaseModel]) -> dict[str, Any]:
    """Compact field-description outline for vision models that echo verbose JSON Schemas."""
    shape: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        inner = get_args(field.annotation)
        nested = next(
            (kind for kind in inner if isinstance(kind, type) and issubclass(kind, BaseModel)), None
        )
        shape[name] = [schema_outline(nested)] if nested else (field.description or name)
    return shape


def _default_gemini_thinking_level(model: str, is_retry: bool) -> genai_types.ThinkingLevel:
    return genai_types.ThinkingLevel.MEDIUM if is_retry else genai_types.ThinkingLevel.LOW


class GeminiNativePdfExtractor:
    """Sends a single-page native PDF directly to Gemini with key rotation and backoff on 429/503."""

    provider_name = "gemini"

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
        is_retry_role: bool = False,
    ) -> None:
        if not api_keys:
            raise ValueError("GeminiNativePdfExtractor requires at least one API key")
        self.model_name = model
        self._max_output_tokens = max_output_tokens
        self._is_retry_role = is_retry_role
        timeout_ms = int(timeout_seconds * 1000)
        http_opts = genai_types.HttpOptions(
            timeout=timeout_ms,
            retry_options=genai_types.HttpRetryOptions(attempts=1),
        )
        self._clients = [genai.Client(api_key=key, http_options=http_opts) for key in api_keys]
        self._key_cursor = 0

    def _next_client(self) -> genai.Client:
        client = self._clients[self._key_cursor % len(self._clients)]
        self._key_cursor += 1
        return client

    @staticmethod
    def _parse_gemini_response(response: Any) -> tuple[PrintedPage, TokenCount]:
        raw_text = response.text or "{}"
        printed = PrintedPage.model_validate_json(json_object_text(raw_text))
        usage_meta = response.usage_metadata
        input_tokens = int(getattr(usage_meta, "prompt_token_count", 0) or 0)
        candidates_tokens = int(getattr(usage_meta, "candidates_token_count", 0) or 0)
        thoughts_tokens = int(getattr(usage_meta, "thoughts_token_count", 0) or 0)
        return printed, TokenCount(
            input_tokens=input_tokens,
            output_tokens=candidates_tokens + thoughts_tokens,
        )

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        user_prompt = (
            build_retry_user_prompt(page.page_number, retry_feedback)
            if retry_feedback
            else build_page_user_prompt(page.page_number, total_pages)
        )
        pdf_part = genai_types.Part.from_bytes(data=page.pdf_bytes, mime_type="application/pdf")
        thinking_level = _default_gemini_thinking_level(
            self.model_name, bool(retry_feedback) or self._is_retry_role
        )
        config = genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_EXTRACTION_PROMPT,
            temperature=0.0,
            max_output_tokens=self._max_output_tokens,
            response_mime_type="application/json",
            response_schema=PrintedPage,
            thinking_config=genai_types.ThinkingConfig(thinking_level=thinking_level),
        )

        max_attempts = max(4, len(self._clients) * 2)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = await self._next_client().aio.models.generate_content(
                    model=self.model_name,
                    contents=[pdf_part, user_prompt],
                    config=config,
                )
                return self._parse_gemini_response(response)
            except genai_errors.APIError as exc:
                last_error = exc
                status_code = getattr(exc, "code", None)
                if status_code not in (429, 500, 502, 503, 504) or attempt == max_attempts - 1:
                    break
                delay = min(2.0 * (2**attempt), 20.0)
                logger.warning(
                    "Gemini call failed on page %s (status=%s, attempt=%s/%s); rotating key in %.1fs",
                    page.page_number,
                    status_code,
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
            except ValidationError as exc:
                last_error = exc
                logger.warning("Gemini invalid schema on page %s: %s", page.page_number, exc.errors()[0])
                break
            except Exception as exc:
                last_error = exc
                if attempt == max_attempts - 1:
                    break
                await asyncio.sleep(min(2.0 * (2**attempt), 10.0))

        raise UpstreamUnavailable(
            f"Gemini extraction failed for page {page.page_number} with model {self.model_name}: {last_error}"
        ) from last_error

    async def close(self) -> None:
        for client in self._clients:
            await client.aio.aclose()
            client.close()


class OpenAICompatVisionExtractor:
    """Sends a rendered page PNG to any OpenAI-compatible vision endpoint (e.g. DeepSeek, OpenAI)."""

    provider_name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
    ) -> None:
        self.model_name = model
        self._max_output_tokens = max_output_tokens
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=2,
        )
        self._outline_json = json.dumps(schema_outline(PrintedPage), ensure_ascii=False)

    async def extract_page(
        self,
        page: PdfPagePayload,
        total_pages: int,
        retry_feedback: list[str] | None = None,
    ) -> tuple[PrintedPage, TokenCount]:
        user_prompt = (
            build_retry_user_prompt(page.page_number, retry_feedback)
            if retry_feedback
            else build_page_user_prompt(page.page_number, total_pages)
        )
        png_b64 = "data:image/png;base64," + base64.b64encode(page.png_bytes).decode("ascii")
        user_parts: list[dict[str, Any]] = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": png_b64}},
        ]
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_EXTRACTION_PROMPT}\n\n"
                    f"Return exactly one JSON object matching this shape:\n{self._outline_json}"
                ),
            },
            cast(ChatCompletionMessageParam, {"role": "user", "content": user_parts}),
        ]

        disable_thinking = "deepseek" in self.model_name.lower()
        first_text, first_tokens = await self._complete(messages, disable_thinking=disable_thinking)
        try:
            return PrintedPage.model_validate_json(json_object_text(first_text)), first_tokens
        except ValidationError as exc:
            logger.warning(
                "Vision model output failed validation on page %s, requesting one repair: %s",
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

        repaired_text, repair_tokens = await self._complete(messages, disable_thinking=disable_thinking)
        total_tokens = TokenCount(
            input_tokens=first_tokens.input_tokens + repair_tokens.input_tokens,
            output_tokens=first_tokens.output_tokens + repair_tokens.output_tokens,
        )
        try:
            return PrintedPage.model_validate_json(json_object_text(repaired_text)), total_tokens
        except ValidationError as exc:
            raise UpstreamUnavailable(
                f"Vision model {self.model_name} did not return valid JSON for page {page.page_number}"
            ) from exc

    async def _complete(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        disable_thinking: bool,
    ) -> tuple[str, TokenCount]:
        extra_body = {"thinking": {"type": "disabled"}} if disable_thinking else None
        try:
            response = await self._client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=0.0,
                max_tokens=self._max_output_tokens,
                response_format={"type": "json_object"},
                extra_body=extra_body,
            )
        except OpenAIAPIError as exc:
            raise UpstreamUnavailable(f"Vision provider call failed for {self.model_name}: {exc}") from exc

        usage = response.usage
        tokens = TokenCount(
            input_tokens=int(usage.prompt_tokens) if usage and usage.prompt_tokens else 0,
            output_tokens=int(usage.completion_tokens) if usage and usage.completion_tokens else 0,
        )
        content = response.choices[0].message.content or "{}"
        return content, tokens

    async def close(self) -> None:
        await self._client.close()


class DiskCachedExtractor:
    """Caches page extraction JSON on disk keyed by page bytes, provider, model, and prompt."""

    def __init__(self, inner: PageExtractor, cache_dir: Path | str) -> None:
        self._inner = inner
        self.provider_name = inner.provider_name
        self.model_name = inner.model_name
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, page: PdfPagePayload, retry_feedback: list[str] | None) -> Path:
        hasher = hashlib.sha256()
        hasher.update(self.provider_name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(self.model_name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(SYSTEM_EXTRACTION_PROMPT.encode("utf-8"))
        hasher.update(b"\0")
        # Hash the deterministic rendered PNG bytes because PyMuPDF sub-PDF tobytes() embeds a fresh trailer ID each run.
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
        path = self._cache_path(page, retry_feedback)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return PrintedPage.model_validate(payload["printed"]), TokenCount(0, 0, from_cache=True)

        printed, tokens = await self._inner.extract_page(page, total_pages, retry_feedback=retry_feedback)
        record = {
            "provider": self.provider_name,
            "model": self.model_name,
            "page_number": page.page_number,
            "recorded_input_tokens": tokens.input_tokens,
            "recorded_output_tokens": tokens.output_tokens,
            "printed": printed.model_dump(mode="json"),
        }
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return printed, tokens

    async def close(self) -> None:
        await self._inner.close()


def estimate_cost_usd(
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

    cost = (
        primary_input_tokens * settings.llm_input_price_per_million_usd
        + primary_output_tokens * settings.llm_output_price_per_million_usd
    ) / 1_000_000.0

    retry_in_price = settings.llm_retry_input_price_per_million_usd
    retry_out_price = settings.llm_retry_output_price_per_million_usd
    if retry_input_tokens or retry_output_tokens:
        if retry_in_price is None:
            retry_in_price = settings.llm_input_price_per_million_usd
        if retry_out_price is None:
            retry_out_price = settings.llm_output_price_per_million_usd
        cost += (retry_input_tokens * retry_in_price + retry_output_tokens * retry_out_price) / 1_000_000.0

    return round(cost, 6)


def build_extractors(settings: Settings) -> tuple[PageExtractor, PageExtractor | None]:
    """Instantiate primary and optional targeted-retry extractors once at application startup."""
    api_keys = settings.resolve_api_keys()

    def make_one(model_name: str, is_retry_role: bool) -> PageExtractor:
        extractor: PageExtractor
        if settings.llm_provider == "gemini":
            extractor = GeminiNativePdfExtractor(
                api_keys=api_keys,
                model=model_name,
                timeout_seconds=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
                is_retry_role=is_retry_role,
            )
        else:
            extractor = OpenAICompatVisionExtractor(
                api_key=api_keys[0],
                model=model_name,
                base_url=settings.llm_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                max_output_tokens=settings.llm_max_output_tokens,
            )
        if settings.extraction_cache_dir:
            extractor = DiskCachedExtractor(extractor, settings.extraction_cache_dir)
        return extractor

    primary = make_one(settings.llm_model, is_retry_role=False)
    retry = make_one(settings.llm_retry_model, is_retry_role=True) if settings.llm_retry_model else None
    return primary, retry
