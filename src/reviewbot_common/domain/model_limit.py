import re

_MODEL_LIMIT_ERROR_PATTERNS = (
    re.compile(r"\b(?:session|usage|rate|context|token|request|weekly|daily|monthly) limit\b"),
    re.compile(r"\b(?:context window|maximum context)\b"),
    re.compile(r"\btoo many tokens\b"),
    re.compile(r"\b(?:input|prompt) is too long\b"),
    re.compile(r"\b(?:quota exceeded|quota reached|usage quota|request quota|out of quota)\b"),
    re.compile(r"\bresource_exhausted\b"),
    re.compile(r"사용량 한도"),
    re.compile(r"토큰 한도"),
    re.compile(r"컨텍스트 한도"),
    re.compile(r"입력 한도"),
    re.compile(r"요청 한도"),
    re.compile(r"할당량 초과"),
    re.compile(r"한도를 초과"),
)

_GLOBAL_MODEL_LIMIT_ERROR_PATTERNS = (
    re.compile(r"\b(?:session|usage|rate|request|weekly|daily|monthly) limit\b"),
    re.compile(r"\b(?:quota exceeded|quota reached|usage quota|request quota|out of quota)\b"),
    re.compile(r"\bresource_exhausted\b"),
    re.compile(r"사용량 한도"),
    re.compile(r"요청 한도"),
    re.compile(r"할당량 초과"),
)

_MODEL_LIMIT_RESET_HINT_PATTERNS = (
    re.compile(r"\bresets?\s+(?:at\s+|in\s+)?(?P<hint>[^.\n]+)", re.IGNORECASE),
    re.compile(r"재설정[^\n:：]*[:：]?\s*(?P<hint>[^\n.]+)"),
)


def is_model_limit_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(pattern.search(text) is not None for pattern in _MODEL_LIMIT_ERROR_PATTERNS)


def is_global_model_limit_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(pattern.search(text) is not None for pattern in _GLOBAL_MODEL_LIMIT_ERROR_PATTERNS)


def extract_model_limit_reset_hint(text: str) -> str | None:
    for pattern in _MODEL_LIMIT_RESET_HINT_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            hint = match.group("hint").strip(" ·:-")
            if hint:
                return hint
    return None
