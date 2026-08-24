from __future__ import annotations

from ox_alpha_review.application.guarded_completion import GuardedCompletion
from ox_alpha_review.application.ports import (
    OpenRouterTransportError,
    QuotaExceededError,
    ReadinessError,
    StateSafetyError,
)
from ox_alpha_review.domain import CompletionPayload, SafetyViolation
from reviewbot_common.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    PullRequest,
    ReviewHistory,
    ReviewResult,
)
from reviewbot_common.interfaces import ReviewEngineError

from .ox_alpha_parser import parse_review
from .ox_alpha_prompt import DIFF_MODE_SYSTEM_RULES, SYSTEM_RULES, build_prompt


class OxAlphaReviewEngine:
    def __init__(
        self,
        completion: GuardedCompletion,
        *,
        max_output_tokens: int = 16_384,
    ) -> None:
        self._completion = completion
        self._max_output_tokens = max_output_tokens

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        prompt = build_prompt(pr, dump, history=history)
        system_rules = (
            DIFF_MODE_SYSTEM_RULES if dump.mode == DUMP_MODE_DIFF else SYSTEM_RULES
        ).strip()
        if not prompt.startswith(system_rules):
            raise RuntimeError("review prompt does not start with the trusted system rules")
        untrusted_context = prompt[len(system_rules) :].lstrip()
        try:
            response = await self._completion.complete(
                CompletionPayload(
                    messages=(
                        {"role": "system", "content": system_rules},
                        {"role": "user", "content": untrusted_context},
                    ),
                    max_tokens=self._max_output_tokens,
                )
            )
        except OpenRouterTransportError as exc:
            if exc.context_limit:
                raise ReviewEngineError("Ox Alpha maximum context length exceeded") from exc
            raise _non_retryable_error(
                "Ox Alpha 요청 결과를 안전하게 확인할 수 없어 추론을 중단했습니다."
            ) from exc
        except QuotaExceededError as exc:
            raise _non_retryable_error(
                "Ox Alpha 무료 24시간 시도 한도가 소진되어 추론을 중단했습니다."
            ) from exc
        except (ReadinessError, StateSafetyError, SafetyViolation) as exc:
            raise _non_retryable_error("Ox Alpha 결제 안전장치가 추론을 차단했습니다.") from exc
        return parse_review(response.content)


def _non_retryable_error(message: str) -> ReviewEngineError:
    return ReviewEngineError(
        message,
        non_limit_failure_message=message,
        allow_diff_fallback=False,
    )
