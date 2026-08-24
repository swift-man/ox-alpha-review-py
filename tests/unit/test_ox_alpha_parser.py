import json

from ox_alpha_review.infrastructure.ox_alpha_parser import _sanitize_body, parse_review
from reviewbot_common.domain import ReviewEvent


def test_sanitize_body_escapes_code_fence_when_wrapping_raw_dict() -> None:
    body = "{'severity': 'major', 'raw': '```boom```'}"

    sanitized = _sanitize_body(body)  # noqa: SLF001

    assert "```boom```" not in sanitized
    assert "`\u200b`\u200b`boom`\u200b`\u200b`" in sanitized


def test_parse_review_continues_after_unbalanced_brace_prefix() -> None:
    payload = {
        "summary": "ok",
        "event": "APPROVE",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [],
    }

    result = parse_review("agent log { broken\n" + json.dumps(payload))

    assert result.summary == "ok"
    assert result.event is ReviewEvent.APPROVE


def test_parse_review_repairs_trailing_commas_in_model_json() -> None:
    raw = """
{
  "summary": "ok",
  "event": "APPROVE",
  "positives": [
    "nice",
  ],
  "must_fix": [],
  "improvements": [],
  "comments": [
    {
      "path": "src/app.py",
      "line": 3,
      "severity": "minor",
      "body": "remove dead branch",
    },
  ],
  "meta_replies": [
    {
      "reply_to_comment_id": 123,
      "body": "agreed",
    },
  ],
}
"""

    result = parse_review(raw)

    assert result.summary == "ok"
    assert result.event is ReviewEvent.APPROVE
    assert result.positives == ("nice",)
    assert len(result.findings) == 1
    assert result.findings[0].body == "remove dead branch"
    assert len(result.meta_replies) == 1


def test_parse_review_suppresses_unparseable_structured_payload() -> None:
    raw = '{"summary": "ok", "event": "APPROVE", "comments": ['

    result = parse_review(raw)

    assert result.event is ReviewEvent.COMMENT
    assert "JSON" in result.summary
    assert '"summary"' not in result.summary


def test_parse_review_normalizes_inline_code_fence_comment_body() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": "문제 → 환경 전체가 전달됩니다. 제안 → 예: ```python\n"
                'allowed = ("PATH", "HOME")\n'
                "``` 이어서 필요한 인증 변수만 추가해 주세요.",
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == (
        "문제 → 환경 전체가 전달됩니다. 제안 → 예:\n"
        "```python\n"
        'allowed = ("PATH", "HOME")\n'
        "```\n"
        "이어서 필요한 인증 변수만 추가해 주세요."
    )


def test_parse_review_escapes_unbalanced_code_fence_comment_body() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": "문제 → 단일 코드펜스(```)가 마크다운을 깨뜨릴 수 있습니다.",
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert "```" not in result.findings[0].body
    assert "`\u200b`\u200b`" in result.findings[0].body


def test_parse_review_ignores_code_fence_literal_inside_code_block() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": '문제 → 예:\n```python\nFENCE = "```"\nprint(FENCE)\n```',
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == ('문제 → 예:\n```python\nFENCE = "```"\nprint(FENCE)\n```')


def test_parse_review_normalizes_single_line_code_fence() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": '제안 → 예: ```python allowed = ("PATH", "HOME") ``` 이어서 설명해 주세요.',
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == (
        '제안 → 예:\n```python\nallowed = ("PATH", "HOME")\n```\n이어서 설명해 주세요.'
    )


def test_parse_review_ignores_spaced_code_fence_literal_on_later_code_line() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": '문제 → 예:\n```python\ntext = "앞 ``` 뒤"\nprint(text)\n```',
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == (
        '문제 → 예:\n```python\ntext = "앞 ``` 뒤"\nprint(text)\n```'
    )


def test_parse_review_ignores_info_string_fence_literal_inside_code_block() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": "문제 → 예:\n```text\n ```python\nprint('nested')\n```",
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == ("문제 → 예:\n```text\n ```python\nprint('nested')\n```")


def test_parse_review_preserves_trailing_spaces_without_fences() -> None:
    payload = {
        "summary": "ok",
        "event": "REQUEST_CHANGES",
        "positives": [],
        "must_fix": [],
        "improvements": [],
        "comments": [
            {
                "path": "src/app.py",
                "line": 3,
                "severity": "major",
                "body": "첫 줄  \n둘째 줄",
            },
        ],
    }

    result = parse_review(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].body == "첫 줄  \n둘째 줄"
