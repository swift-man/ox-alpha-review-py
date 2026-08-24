from decimal import Decimal

import pytest

from ox_alpha_review.domain import (
    CompletionResponse,
    EndpointMetadata,
    FreeOnlyPolicy,
    KeyMetadata,
    ModelMetadata,
    SafetyViolation,
)


def _key(**changes: object) -> KeyMetadata:
    values: dict[str, object] = {
        "is_free_tier": True,
        "is_management_key": False,
        "is_provisioning_key": False,
        "include_byok_in_limit": False,
        "spending_limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "usage": Decimal("0"),
        "byok_usage": Decimal("0"),
    }
    values.update(changes)
    return KeyMetadata(**values)  # type: ignore[arg-type]


def _model(pricing: object | None = None) -> ModelMetadata:
    return ModelMetadata(
        model_id="stealth/ox-alpha",
        canonical_slug="stealth/ox-alpha",
        pricing=pricing or {"prompt": "0", "completion": "0"},
    )


def _endpoint(pricing: object | None = None) -> EndpointMetadata:
    return EndpointMetadata(
        model_id="stealth/ox-alpha",
        provider_name="Stealth",
        provider_slug="stealth",
        pricing=pricing or {"prompt": "0", "completion": "0", "discount": 0},
        status=0,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_free_tier", False),
        ("is_management_key", True),
        ("is_provisioning_key", True),
        ("include_byok_in_limit", True),
        ("spending_limit", Decimal("0.01")),
        ("limit_remaining", Decimal("0.01")),
        ("limit_reset", "monthly"),
        ("usage", Decimal("0.01")),
        ("byok_usage", Decimal("0.01")),
    ],
)
def test_key_policy_fails_closed_on_paid_or_management_state(
    field: str,
    value: object,
) -> None:
    with pytest.raises(SafetyViolation):
        FreeOnlyPolicy().validate_key(_key(**{field: value}))


@pytest.mark.parametrize(
    ("spending_limit", "limit_remaining"),
    [(None, None), (Decimal("0"), Decimal("0"))],
)
def test_key_policy_accepts_only_unlimited_limit_representations(
    spending_limit: Decimal | None,
    limit_remaining: Decimal | None,
) -> None:
    FreeOnlyPolicy().validate_key(
        _key(
            spending_limit=spending_limit,
            limit_remaining=limit_remaining,
        )
    )


@pytest.mark.parametrize(
    ("spending_limit", "limit_remaining"),
    [(Decimal("0"), None), (None, Decimal("0"))],
)
def test_key_policy_rejects_ambiguous_mixed_limit_state(
    spending_limit: Decimal | None,
    limit_remaining: Decimal | None,
) -> None:
    with pytest.raises(SafetyViolation):
        FreeOnlyPolicy().validate_key(
            _key(
                spending_limit=spending_limit,
                limit_remaining=limit_remaining,
            )
        )


def test_catalog_accepts_only_exact_zero_price_endpoint() -> None:
    FreeOnlyPolicy().validate_catalog(_model(), (_endpoint(),))


@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "0.000001", "completion": "0"},
        {"prompt": "0", "completion": "0", "request": "1"},
        {
            "prompt": "0",
            "completion": "0",
            "overrides": [{"min_prompt_tokens": 1000, "prompt": "0", "completion": "0.1"}],
        },
        {"prompt": "0"},
        {"prompt": "0", "completion": None},
    ],
)
def test_catalog_rejects_nonzero_unknown_or_incomplete_pricing(pricing: object) -> None:
    with pytest.raises(SafetyViolation):
        FreeOnlyPolicy().validate_catalog(_model(pricing), (_endpoint(),))


def test_response_requires_exact_identity_and_zero_detailed_cost() -> None:
    FreeOnlyPolicy().validate_response(
        CompletionResponse(
            content="{}",
            model_id="stealth/ox-alpha",
            provider_slug="Stealth",
            cost=Decimal("0"),
            cost_details={"upstream_inference_cost": 0},
            service_tier="default",
        )
    )


@pytest.mark.parametrize(
    "response",
    [
        CompletionResponse("{}", "other/model", "Stealth", Decimal("0"), {"upstream": 0}, None),
        CompletionResponse("{}", "stealth/ox-alpha", "Other", Decimal("0"), {"upstream": 0}, None),
        CompletionResponse(
            "{}", "stealth/ox-alpha", "Stealth", Decimal("0.1"), {"upstream": 0}, None
        ),
        CompletionResponse(
            "{}", "stealth/ox-alpha", "Stealth", Decimal("0"), {"upstream": 1}, None
        ),
        CompletionResponse("{}", "stealth/ox-alpha", "Stealth", Decimal("0"), {}, None),
    ],
)
def test_response_policy_fails_closed(response: CompletionResponse) -> None:
    with pytest.raises(SafetyViolation):
        FreeOnlyPolicy().validate_response(response)
