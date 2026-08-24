from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

EXACT_MODEL_ID = "stealth/ox-alpha"
EXACT_PROVIDER_SLUG = "stealth"
MAX_PRICE_FIELDS = ("prompt", "completion", "request", "image")


class SafetyViolation(RuntimeError):
    """A fail-closed violation that must prevent paid inference."""


@dataclass(frozen=True)
class KeyMetadata:
    is_free_tier: bool
    is_management_key: bool
    is_provisioning_key: bool
    include_byok_in_limit: bool
    spending_limit: Decimal | None
    limit_remaining: Decimal | None
    limit_reset: str | None
    usage: Decimal
    byok_usage: Decimal


@dataclass(frozen=True)
class ModelMetadata:
    model_id: str
    canonical_slug: str
    pricing: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "pricing", MappingProxyType(dict(self.pricing)))


@dataclass(frozen=True)
class EndpointMetadata:
    model_id: str
    provider_name: str
    provider_slug: str
    pricing: Mapping[str, object]
    status: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pricing", MappingProxyType(dict(self.pricing)))


@dataclass(frozen=True)
class CompletionPayload:
    messages: tuple[Mapping[str, str], ...]
    max_tokens: int


@dataclass(frozen=True)
class CompletionResponse:
    content: str
    model_id: str
    provider_slug: str
    cost: Decimal
    cost_details: Mapping[str, object]
    service_tier: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cost_details", MappingProxyType(dict(self.cost_details)))


class FreeOnlyPolicy:
    """Pure policy for the immutable Ox Alpha zero-cost allowlist."""

    def validate_key(self, metadata: KeyMetadata) -> None:
        if metadata.is_management_key or metadata.is_provisioning_key:
            raise SafetyViolation("OpenRouter management/provisioning keys are forbidden")
        if not metadata.is_free_tier:
            raise SafetyViolation("OpenRouter key is not a Free-tier key")
        if metadata.include_byok_in_limit or metadata.byok_usage != Decimal("0"):
            raise SafetyViolation("OpenRouter BYOK state is forbidden")
        if metadata.usage != Decimal("0"):
            raise SafetyViolation("OpenRouter key has nonzero credit usage")

        # OpenRouter documents null as an unlimited key and its dashboard also uses
        # zero to mean unlimited. Neither representation is a hard-zero cost guard.
        # We allow those two internally consistent representations only because the
        # actual pre-authorization barrier is provider.max_price=0 plus the separate
        # written production-acceptance gate. Any positive, resettable, or mixed
        # limit state authorizes an unknown credit path and therefore fails closed.
        limits = (metadata.spending_limit, metadata.limit_remaining)
        if limits not in ((None, None), (Decimal("0"), Decimal("0"))):
            raise SafetyViolation("OpenRouter key credit limit state is unsafe")
        if metadata.limit_reset is not None:
            raise SafetyViolation("OpenRouter key has a resettable credit limit")

    def validate_catalog(
        self,
        model: ModelMetadata,
        endpoints: Sequence[EndpointMetadata],
    ) -> None:
        if model.model_id != EXACT_MODEL_ID or model.canonical_slug != EXACT_MODEL_ID:
            raise SafetyViolation("OpenRouter model identity drift")
        _validate_zero_pricing(model.pricing)
        if not endpoints:
            raise SafetyViolation("OpenRouter has no Ox Alpha endpoint")
        matching = tuple(
            endpoint
            for endpoint in endpoints
            if endpoint.provider_slug == EXACT_PROVIDER_SLUG and endpoint.model_id == EXACT_MODEL_ID
        )
        if len(matching) != 1:
            raise SafetyViolation("OpenRouter Stealth endpoint identity is not unique")
        endpoint = matching[0]
        if endpoint.provider_name.casefold() != "stealth" or endpoint.status != 0:
            raise SafetyViolation("OpenRouter Stealth endpoint is unavailable or renamed")
        _validate_zero_pricing(endpoint.pricing)

    def validate_response(self, response: CompletionResponse) -> None:
        if response.model_id != EXACT_MODEL_ID:
            raise SafetyViolation("completion response model identity mismatch")
        if response.provider_slug.casefold() != EXACT_PROVIDER_SLUG:
            raise SafetyViolation("completion response provider identity mismatch")
        if response.service_tier not in (None, "default"):
            raise SafetyViolation("non-default OpenRouter service tier was used")
        if response.cost != Decimal("0"):
            raise SafetyViolation("completion response reported nonzero cost")
        _validate_cost_details(response.cost_details)

    def provider_preferences(self) -> dict[str, object]:
        return {
            "order": [EXACT_PROVIDER_SLUG],
            "only": [EXACT_PROVIDER_SLUG],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {field: "0" for field in MAX_PRICE_FIELDS},
        }


def decimal_from_api(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SafetyViolation(f"{field} is missing or non-numeric")
    if not isinstance(value, (str, int, float, Decimal)):
        raise SafetyViolation(f"{field} is non-numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise SafetyViolation(f"{field} is non-numeric") from exc
    if not result.is_finite():
        raise SafetyViolation(f"{field} is non-finite")
    return result


def _validate_zero_pricing(pricing: Mapping[str, object]) -> None:
    if "prompt" not in pricing or "completion" not in pricing:
        raise SafetyViolation("pricing lacks prompt/completion fields")
    _validate_pricing_mapping(pricing)


def _validate_pricing_mapping(pricing: Mapping[str, object]) -> None:
    threshold_fields = {
        "min_prompt_tokens",
        "max_prompt_tokens",
        "min_completion_tokens",
        "max_completion_tokens",
    }
    for field, value in pricing.items():
        if field == "overrides":
            if not isinstance(value, list):
                raise SafetyViolation("pricing overrides have an unknown shape")
            for override in value:
                if not isinstance(override, Mapping):
                    raise SafetyViolation("pricing override is not an object")
                _validate_pricing_mapping(override)
            continue
        if field in threshold_fields:
            if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
                raise SafetyViolation(f"pricing threshold {field} is invalid")
            continue
        if isinstance(value, (Mapping, list)):
            raise SafetyViolation(f"unknown nested pricing field: {field}")
        if decimal_from_api(value, field=f"pricing.{field}") != Decimal("0"):
            raise SafetyViolation(f"nonzero OpenRouter price: {field}")


def _validate_cost_details(cost_details: Mapping[str, object]) -> None:
    if not cost_details:
        raise SafetyViolation("completion response lacks detailed cost accounting")
    for field, value in cost_details.items():
        if decimal_from_api(value, field=f"usage.cost_details.{field}") != Decimal("0"):
            raise SafetyViolation(f"completion response reported nonzero {field}")
