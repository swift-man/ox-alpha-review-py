# Compatibility matrix

기준 커밋:

- Claude: `swift-man/claude-pr-review-py@7de1ac65850b12c7ee82f17710a374c41ad494df`
- Gemini: `swift-man/gemini-pr-review-py@69055fb7922c8c225b2853dea2dd71f43ab60208`
- Codex: `swift-man/codex-pr-review-py@dda9fb8aac97dcecb4a56dfbaceb2db2ef48f89c`

| 항목 | Ox Alpha 구현 | 판정 근거 |
|---|---|---|
| opened/synchronize/reopened/ready_for_review | 동일 | 세 기준의 공통 webhook 동작 |
| draft 건너뜀 | 동일 | 세 기준의 공통 동작 |
| 변경 파일 우선 전체 tracked 저장소 컨텍스트 | 동일 | 프로젝트 `AGENTS.md` 우선 규칙 |
| diff-only | 사전 예산 초과 또는 명시적 context-limit 응답에서만 사용 | 프로젝트 `AGENTS.md` 우선 규칙 |
| RIGHT-side inline 필터와 review body fallback | 동일 | 고정 Claude 공통 패키지 |
| 한국어 review/history/follow-up/meta reply | 동일 | 고정 Claude 공통 패키지와 prompt/parser |
| 모델 transport | OpenRouter direct text-only HTTP | provider 고유 차이이며 호환 대상 아님 |
| 모델/provider | `stealth/ox-alpha` / `stealth`만 허용 | 프로젝트 free-only 규칙 |
| provider fallback/transport retry | 모두 비활성 | 프로젝트 free-only 규칙이 가용성보다 우선 |
| context-limit 재검토 | 오류 분류와 router `attempt=0`/endpoint 미선택이 함께 검증된 경우 diff-only 1회 | 기존 context fallback 호환; 각 시도를 별도 쿼터 예약 |
| completion 전 key/catalog 검증 | 추가 안전 게이트 | 프로젝트 free-only 규칙 |
| 45회/24시간 로컬 예약 | 추가 안전 게이트 | 프로젝트 free-only 규칙 |
| persistent delivery/latch/readiness/미검증 시도 journal | 추가 안전 게이트 | 프로젝트 free-only 규칙 |
| 서버 다중 인스턴스 | OS process lock으로 거부 | v0.1 직렬화·저장소 격리 규칙 |
| `DRY_RUN=true` | GitHub 쓰기와 OpenRouter 추론 모두 차단 | 결제·비공개 코드 보호 우선 규칙 |
| production readiness | 서면 max-price pre-authorization 확인 전 503 | 프로젝트 free-only 규칙 |

현재 명세 간 미해결 충돌은 없습니다. provider 로그인, account rotation, CLI fallback과
각 기준 봇의 모델 라벨은 명시적으로 호환 대상에서 제외했습니다.
