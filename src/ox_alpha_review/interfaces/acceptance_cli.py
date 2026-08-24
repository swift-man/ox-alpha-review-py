from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

import httpx

from ox_alpha_review.config import load_settings
from ox_alpha_review.domain import FreeOnlyPolicy
from ox_alpha_review.infrastructure.openrouter import (
    OPENROUTER_ORIGIN,
    OpenRouterKeyMetadataReader,
    OpenRouterModelCatalogReader,
)
from ox_alpha_review.infrastructure.runtime_primitives import SystemClock
from ox_alpha_review.infrastructure.sqlite_state import (
    SQLiteProductionReadiness,
    SQLiteSafetyDatabase,
)
from reviewbot_common.infrastructure.github_app_client import _default_tls_context

_MAX_CONFIRMATION_BYTES = 1024 * 1024
_REQUIRED_CONFIRMATION_TERMS = (
    "openrouter",
    "provider.max_price",
    "limit 0",
    "unlimited",
    "pre-authorization",
    "nonzero price",
    "overage",
    "negative balance",
    "invoice",
    "payment method",
    "purchased credits",
    "auto top-up",
    "byok",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record OpenRouter's written max-price billing confirmation after live "
            "free-only metadata validation. This command never sends a completion."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--confirmation-file",
        type=Path,
        help="OpenRouter's written confirmation, stored outside this repository",
    )
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="validate current key/model/endpoint metadata without recording acceptance",
    )
    return parser.parse_args()


async def _accept(
    confirmation_file: Path | None,
    *,
    check_only: bool,
) -> None:
    settings = load_settings()
    policy = FreeOnlyPolicy()
    async with httpx.AsyncClient(
        base_url=OPENROUTER_ORIGIN,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=30.0,
        verify=_default_tls_context(),
        trust_env=False,
    ) as client:
        key = await OpenRouterKeyMetadataReader(client).read()
        policy.validate_key(key)
        model, endpoints = await OpenRouterModelCatalogReader(client).read()
        policy.validate_catalog(model, endpoints)
    if check_only:
        print("OpenRouter free-only key/model/endpoint metadata validation passed.")
        return
    if confirmation_file is None:
        raise RuntimeError("confirmation file is required to record acceptance")

    confirmation = _read_confirmation(confirmation_file, repository_root=Path.cwd())

    database = SQLiteSafetyDatabase(settings.state_database)
    await database.initialize()
    readiness = SQLiteProductionReadiness(database)
    key_fingerprint = hashlib.sha256(settings.openrouter_api_key.encode("utf-8")).hexdigest()
    await readiness.record(
        key_fingerprint=key_fingerprint,
        confirmation_sha256=hashlib.sha256(confirmation).hexdigest(),
        accepted_at=SystemClock().now(),
    )
    print("Production acceptance recorded after free-only metadata validation.")


def _read_confirmation(confirmation_file: Path, *, repository_root: Path) -> bytes:
    confirmation_path = confirmation_file.expanduser().resolve(strict=True)
    repository_root = repository_root.resolve()
    if confirmation_path == repository_root or repository_root in confirmation_path.parents:
        raise RuntimeError("confirmation file must be stored outside the repository")
    if confirmation_path.stat().st_size > _MAX_CONFIRMATION_BYTES:
        raise RuntimeError("confirmation file is too large")
    confirmation = confirmation_path.read_bytes()
    try:
        text = confirmation.decode("utf-8").casefold()
    except UnicodeDecodeError as exc:
        raise RuntimeError("written confirmation must be valid UTF-8") from exc
    missing = tuple(term for term in _REQUIRED_CONFIRMATION_TERMS if term not in text)
    if missing:
        raise RuntimeError(
            "written confirmation does not contain every required max-price billing term"
        )
    return confirmation


def run() -> None:
    args = _arguments()
    asyncio.run(
        _accept(
            args.confirmation_file,
            check_only=bool(args.check_only),
        )
    )


if __name__ == "__main__":
    run()
