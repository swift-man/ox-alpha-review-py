from pathlib import Path

import pytest

from ox_alpha_review.interfaces.acceptance_cli import (
    _REQUIRED_CONFIRMATION_TERMS,
    _read_confirmation,
)


def test_confirmation_requires_every_billing_safety_term(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    confirmation = tmp_path / "confirmation.txt"
    confirmation.write_text("OpenRouter provider.max_price", encoding="utf-8")

    with pytest.raises(RuntimeError, match="every required"):
        _read_confirmation(confirmation, repository_root=repository)


def test_confirmation_must_stay_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    confirmation = repository / "confirmation.txt"
    confirmation.write_text("\n".join(_REQUIRED_CONFIRMATION_TERMS), encoding="utf-8")

    with pytest.raises(RuntimeError, match="outside"):
        _read_confirmation(confirmation, repository_root=repository)


def test_valid_confirmation_is_returned_for_hashing(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    confirmation = tmp_path / "confirmation.txt"
    expected = "\n".join(_REQUIRED_CONFIRMATION_TERMS).encode()
    confirmation.write_bytes(expected)

    assert _read_confirmation(confirmation, repository_root=repository) == expected
