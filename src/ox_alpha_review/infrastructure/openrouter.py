from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

import httpx

from ox_alpha_review.application.ports import OpenRouterTransportError
from ox_alpha_review.domain import (
    EXACT_MODEL_ID,
    CompletionPayload,
    CompletionResponse,
    EndpointMetadata,
    FreeOnlyPolicy,
    KeyMetadata,
    ModelMetadata,
    SafetyViolation,
)
from ox_alpha_review.domain.free_only import decimal_from_api

OPENROUTER_ORIGIN = "https://openrouter.ai"
_KEY_PATH = "/api/v1/key"
_MODELS_PATH = "/api/v1/models"
_ENDPOINTS_PATH = "/api/v1/models/stealth/ox-alpha/endpoints"
_COMPLETION_PATH = "/api/v1/chat/completions"
_MAX_PREFLIGHT_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_COMPLETION_RESPONSE_BYTES = 4 * 1024 * 1024
_CONTEXT_LIMIT_MESSAGES = (
    "maximum context length",
    "context length exceeded",
    "context window exceeded",
    "input is too long",
    "prompt is too long",
    "too many input tokens",
)


class OpenRouterKeyMetadataReader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def read(self) -> KeyMetadata:
        payload = await _get_json(self._client, _KEY_PATH)
        data = _mapping_field(payload, "data")
        return KeyMetadata(
            is_free_tier=_bool_field(data, "is_free_tier"),
            is_management_key=_bool_field(data, "is_management_key"),
            is_provisioning_key=_bool_field(data, "is_provisioning_key"),
            include_byok_in_limit=_bool_field(data, "include_byok_in_limit"),
            spending_limit=_nullable_decimal_field(data, "limit"),
            limit_remaining=_nullable_decimal_field(data, "limit_remaining"),
            limit_reset=_nullable_string_field(data, "limit_reset"),
            usage=decimal_from_api(data.get("usage"), field="key.usage"),
            byok_usage=decimal_from_api(data.get("byok_usage"), field="key.byok_usage"),
        )


class OpenRouterModelCatalogReader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def read(self) -> tuple[ModelMetadata, tuple[EndpointMetadata, ...]]:
        models_payload = await _get_json(self._client, _MODELS_PATH)
        raw_models = models_payload.get("data")
        if not isinstance(raw_models, list):
            raise SafetyViolation("OpenRouter model catalog data is not a list")
        exact_models = [
            item
            for item in raw_models
            if isinstance(item, Mapping) and item.get("id") == EXACT_MODEL_ID
        ]
        if len(exact_models) != 1:
            raise SafetyViolation("OpenRouter exact model catalog entry is not unique")
        raw_model = exact_models[0]
        model = ModelMetadata(
            model_id=_str_field(raw_model, "id"),
            canonical_slug=_str_field(raw_model, "canonical_slug"),
            pricing=_mapping_field(raw_model, "pricing"),
        )

        endpoint_payload = await _get_json(self._client, _ENDPOINTS_PATH)
        endpoint_data = _mapping_field(endpoint_payload, "data")
        if _str_field(endpoint_data, "id") != EXACT_MODEL_ID:
            raise SafetyViolation("OpenRouter endpoint catalog model identity mismatch")
        raw_endpoints = endpoint_data.get("endpoints")
        if not isinstance(raw_endpoints, list):
            raise SafetyViolation("OpenRouter endpoint catalog data is not a list")
        endpoints: list[EndpointMetadata] = []
        for raw_endpoint in raw_endpoints:
            if not isinstance(raw_endpoint, Mapping):
                raise SafetyViolation("OpenRouter endpoint entry is not an object")
            status = raw_endpoint.get("status")
            if isinstance(status, bool) or not isinstance(status, int):
                raise SafetyViolation("OpenRouter endpoint status is invalid")
            endpoints.append(
                EndpointMetadata(
                    model_id=_str_field(raw_endpoint, "model_id"),
                    provider_name=_str_field(raw_endpoint, "provider_name"),
                    provider_slug=_str_field(raw_endpoint, "tag"),
                    pricing=_mapping_field(raw_endpoint, "pricing"),
                    status=status,
                )
            )
        return model, tuple(endpoints)


class OpenRouterCompletionTransport:
    """The only adapter that may POST to OpenRouter's completion endpoint."""

    def __init__(self, client: httpx.AsyncClient, policy: FreeOnlyPolicy) -> None:
        self._client = client
        self._policy = policy

    async def complete(self, payload: CompletionPayload) -> CompletionResponse:
        if payload.max_tokens <= 0 or payload.max_tokens > 16_384:
            raise SafetyViolation("completion output bound is invalid")
        request_body: dict[str, object] = {
            "model": EXACT_MODEL_ID,
            "messages": [dict(message) for message in payload.messages],
            "max_tokens": payload.max_tokens,
            "stream": False,
            "provider": self._policy.provider_preferences(),
            "reasoning": {"effort": "high", "exclude": True},
            "response_format": {"type": "json_object"},
        }
        raw = await _request_json(
            self._client,
            "POST",
            _COMPLETION_PATH,
            context="completion",
            max_bytes=_MAX_COMPLETION_RESPONSE_BYTES,
            body=request_body,
        )
        return _parse_completion(raw)


async def _get_json(client: httpx.AsyncClient, path: str) -> Mapping[str, object]:
    return await _request_json(
        client,
        "GET",
        path,
        context="safety preflight",
        max_bytes=_MAX_PREFLIGHT_RESPONSE_BYTES,
    )


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    context: str,
    max_bytes: int,
    body: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    try:
        async with client.stream(method, path, json=body) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise SafetyViolation(f"OpenRouter {context} response exceeded size limit")
            if response.status_code < 200 or response.status_code >= 300:
                context_limit = context == "completion" and _is_context_limit_error(
                    response.status_code, content
                )
                raise OpenRouterTransportError(
                    f"OpenRouter {context} request failed",
                    status_code=response.status_code,
                    context_limit=context_limit,
                    pre_inference_rejection=(
                        context_limit and _is_verified_pre_inference_rejection(content)
                    ),
                )
    except (OpenRouterTransportError, SafetyViolation):
        raise
    except httpx.HTTPError as exc:
        raise OpenRouterTransportError(f"OpenRouter {context} transport failed") from exc
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyViolation(f"OpenRouter {context} returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        raise SafetyViolation(f"OpenRouter {context} response is not an object")
    return cast(Mapping[str, object], raw)


def _is_context_limit_error(status_code: int, content: bytes | bytearray) -> bool:
    if status_code not in (400, 413):
        return False
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, Mapping):
        return False
    error = raw.get("error")
    if not isinstance(error, Mapping):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    normalized = message.casefold()
    return any(marker in normalized for marker in _CONTEXT_LIMIT_MESSAGES)


def _is_verified_pre_inference_rejection(content: bytes | bytearray) -> bool:
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, Mapping):
        return False
    metadata = raw.get("openrouter_metadata")
    if not isinstance(metadata, Mapping):
        return False
    attempt = metadata.get("attempt")
    if isinstance(attempt, bool) or attempt != 0:
        return False
    if metadata.get("requested") != EXACT_MODEL_ID or metadata.get("strategy") != "direct":
        return False
    endpoints = metadata.get("endpoints")
    if not isinstance(endpoints, Mapping):
        return False
    available = endpoints.get("available")
    if not isinstance(available, list) or len(available) != 1:
        return False
    endpoint = available[0]
    if not isinstance(endpoint, Mapping):
        return False
    provider = endpoint.get("provider")
    return (
        isinstance(provider, str)
        and provider.casefold() == "stealth"
        and endpoint.get("model") == EXACT_MODEL_ID
        and endpoint.get("selected") is False
    )


def _parse_completion(raw: Mapping[str, object]) -> CompletionResponse:
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise SafetyViolation("completion response choices are missing or ambiguous")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise SafetyViolation("completion response choice is invalid")
    message = _mapping_field(choice, "message")
    content = _str_field(message, "content")
    usage = _mapping_field(raw, "usage")
    cost_details = _mapping_field(usage, "cost_details")
    provider = _str_field(raw, "provider")
    service_tier = raw.get("service_tier")
    if service_tier is not None and not isinstance(service_tier, str):
        raise SafetyViolation("completion response service tier is invalid")
    return CompletionResponse(
        content=content,
        model_id=_str_field(raw, "model"),
        provider_slug=provider,
        cost=decimal_from_api(usage.get("cost"), field="usage.cost"),
        cost_details=cost_details,
        service_tier=service_tier,
    )


def _mapping_field(data: Mapping[str, object], field: str) -> Mapping[str, object]:
    value = data.get(field)
    if not isinstance(value, Mapping):
        raise SafetyViolation(f"OpenRouter field {field} is missing or invalid")
    return cast(Mapping[str, object], value)


def _str_field(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise SafetyViolation(f"OpenRouter field {field} is missing or invalid")
    return value


def _bool_field(data: Mapping[str, object], field: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise SafetyViolation(f"OpenRouter field {field} is missing or invalid")
    return value


def _nullable_decimal_field(data: Mapping[str, object], field: str) -> Decimal | None:
    if field not in data:
        raise SafetyViolation(f"key.{field} is missing")
    value = data[field]
    if value is None:
        return None
    return decimal_from_api(value, field=f"key.{field}")


def _nullable_string_field(data: Mapping[str, object], field: str) -> str | None:
    if field not in data:
        raise SafetyViolation(f"key.{field} is missing")
    value = data[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SafetyViolation(f"key.{field} is invalid")
    return value
