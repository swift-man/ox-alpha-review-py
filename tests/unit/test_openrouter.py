import json

import httpx
import pytest

from ox_alpha_review.application.ports import OpenRouterTransportError
from ox_alpha_review.domain import CompletionPayload, FreeOnlyPolicy, SafetyViolation
from ox_alpha_review.infrastructure.openrouter import (
    OpenRouterCompletionTransport,
    OpenRouterKeyMetadataReader,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "limit_remaining"),
    [(None, None), (0, 0)],
)
async def test_key_reader_preserves_unlimited_limit_representation(
    limit: int | None,
    limit_remaining: int | None,
) -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "is_free_tier": True,
                    "is_management_key": False,
                    "is_provisioning_key": False,
                    "include_byok_in_limit": False,
                    "limit": limit,
                    "limit_remaining": limit_remaining,
                    "limit_reset": None,
                    "usage": 0,
                    "byok_usage": 0,
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        metadata = await OpenRouterKeyMetadataReader(client).read()

    expected = None if limit is None else 0
    assert metadata.spending_limit == expected
    assert metadata.limit_remaining == expected


@pytest.mark.asyncio
async def test_key_reader_rejects_missing_limit_field() -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "is_free_tier": True,
                    "is_management_key": False,
                    "is_provisioning_key": False,
                    "include_byok_in_limit": False,
                    "limit_remaining": None,
                    "limit_reset": None,
                    "usage": 0,
                    "byok_usage": 0,
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        with pytest.raises(SafetyViolation, match="key.limit"):
            await OpenRouterKeyMetadataReader(client).read()


@pytest.mark.asyncio
async def test_completion_request_is_exact_free_only_snapshot() -> None:
    captured: dict[str, object] = {}

    async def responder(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "stealth/ox-alpha",
                "provider": "Stealth",
                "service_tier": "default",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "cost": 0,
                    "cost_details": {"upstream_inference_cost": 0},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        result = await OpenRouterCompletionTransport(
            client,
            FreeOnlyPolicy(),
        ).complete(
            CompletionPayload(
                messages=({"role": "user", "content": "review"},),
                max_tokens=1024,
            )
        )

    assert result.content == "{}"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"] == {
        "model": "stealth/ox-alpha",
        "messages": [{"role": "user", "content": "review"}],
        "max_tokens": 1024,
        "stream": False,
        "provider": {
            "order": ["stealth"],
            "only": ["stealth"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {
                "prompt": "0",
                "completion": "0",
                "request": "0",
                "image": "0",
            },
        },
        "reasoning": {"effort": "high", "exclude": True},
        "response_format": {"type": "json_object"},
    }
    forbidden = {"tools", "tool_choice", "plugins", "models", "service_tier"}
    assert forbidden.isdisjoint(captured["body"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_completion_rejects_missing_cost_before_returning_content() -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "stealth/ox-alpha",
                "provider": "Stealth",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"cost": 0},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        with pytest.raises(SafetyViolation):
            await OpenRouterCompletionTransport(client, FreeOnlyPolicy()).complete(
                CompletionPayload(
                    messages=({"role": "user", "content": "review"},),
                    max_tokens=1024,
                )
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("This model's maximum context length is 131072 tokens", True),
        ("Provider is temporarily unavailable", False),
    ],
)
async def test_completion_classifies_only_explicit_context_limit_errors(
    message: str,
    expected: bool,
) -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {"code": 400, "message": message},
                "openrouter_metadata": {
                    "requested": "stealth/ox-alpha",
                    "strategy": "direct",
                    "attempt": 0,
                    "endpoints": {
                        "available": [
                            {
                                "provider": "Stealth",
                                "model": "stealth/ox-alpha",
                                "selected": False,
                            }
                        ]
                    },
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        with pytest.raises(OpenRouterTransportError) as captured:
            await OpenRouterCompletionTransport(client, FreeOnlyPolicy()).complete(
                CompletionPayload(
                    messages=({"role": "user", "content": "review"},),
                    max_tokens=1024,
                )
            )

    assert captured.value.context_limit is expected
    assert captured.value.pre_inference_rejection is expected


@pytest.mark.asyncio
async def test_context_error_without_router_proof_is_not_safe_for_fallback() -> None:
    async def responder(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 400, "message": "maximum context length exceeded"}},
        )

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(responder),
    ) as client:
        with pytest.raises(OpenRouterTransportError) as captured:
            await OpenRouterCompletionTransport(client, FreeOnlyPolicy()).complete(
                CompletionPayload(
                    messages=({"role": "user", "content": "review"},),
                    max_tokens=1024,
                )
            )

    assert captured.value.context_limit is True
    assert captured.value.pre_inference_rejection is False
