from reviewbot_common.logging_utils import _redact_arg, redact_text


def test_redact_text_preserves_status_word_after_secret_value() -> None:
    assert redact_text("secret=abc failed") == "secret=*** failed"


def test_redact_text_masks_authorization_bearer_value() -> None:
    assert redact_text("authorization=Bearer ghs_123 failed") == "authorization=*** failed"


def test_redact_text_masks_authorization_token_value() -> None:
    assert redact_text("authorization=token ghs_123 failed") == "authorization=*** failed"


def test_redact_text_masks_url_userinfo_with_common_token_chars() -> None:
    text = "fatal https://x-access-token:ghs_Amd.123@github.com/owner/repo failed"

    assert redact_text(text) == "fatal https://***@github.com/owner/repo failed"


def test_redact_arg_masks_set_elements() -> None:
    assert _redact_arg({"secret=abc", "safe"}) == {"secret=***", "safe"}  # noqa: SLF001
