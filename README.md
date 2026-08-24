# ox-alpha-review-py

`stealth/ox-alpha`만 사용하는 self-hosted GitHub App PR 리뷰 봇입니다. 고정된
Claude/Codex/Gemini 기준 봇과 같은 변경 파일 우선 전체 저장소 리뷰, diff-only
컨텍스트 fallback, RIGHT-side 인라인 댓글, 한국어 리뷰, history/follow-up 흐름을
유지합니다.

## 결제 안전 계약

- 모델은 `stealth/ox-alpha`, provider는 `stealth`로 코드에 고정되어 있습니다.
- provider fallback을 끄고 `prompt`, `completion`, `request`, `image` 가격 상한을
  모두 0으로 보냅니다.
- 각 추론 직전에 키와 모델/endpoint 가격을 다시 확인합니다.
- OpenRouter API의 `limit: null`과 대시보드의 key limit `0`은 모두 무제한을
  뜻하므로 결제 차단 장치로 사용하지 않습니다. 양수/리셋형/불일치 limit 상태는
  차단하지만, 실제 요청 전 가격 차단은 `provider.max_price=0`이 담당합니다.
- 최근 24시간의 추론 시도를 SQLite에 먼저 예약하며 상한은 45회입니다. 실패나 취소도
  환불하지 않습니다.
- completion 전에는 별도의 "미검증 시도"를 SQLite에 기록합니다. 모델/provider/비용 0
  응답을 모두 검증했거나, 명시적 context-limit 오류에 대해 router metadata가
  `attempt=0`과 Stealth endpoint 미선택을 증명한 경우에만 해제합니다. 전송 중 프로세스가
  종료되거나 결과를 확인하지 못하면 재시작 후에도 추론을 계속 차단합니다.
- HTTP 402, 가격/키 상태 drift, 응답 모델/provider 불일치, 비용 0 초과가 발견되면
  영구 안전 latch를 기록하고 이후 추론을 중단합니다.
- v0.1은 OS process lock으로 서버 한 인스턴스만 허용합니다. `uvicorn --workers`나
  동일 state directory를 사용하는 서버 중복 실행은 시작 단계에서 거부됩니다.
- OpenRouter가 `provider.max_price=0`을 비영(0 초과) 가격의 실행 전에 거부하며
  초과 결제·음수 잔액·청구서 경로가 없다고 서면 확인하기 전에는 `/readyz`와
  `/webhook`이 503으로 닫혀 있습니다.
- `DRY_RUN=true`에서는 GitHub 쓰기뿐 아니라 OpenRouter 추론도 차단됩니다. 서면
  acceptance를 기록하고 실제 운영을 시작할 때만 `DRY_RUN=false`로 바꿉니다.

Ox Alpha provider가 프롬프트와 응답을 보관할 수 있으므로 비공개 저장소를 설치하기
전에 해당 데이터 처리 조건을 별도로 검토해야 합니다.

관련 공식 문서:

- [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter router metadata](https://openrouter.ai/docs/guides/features/router-metadata)
- [OpenRouter limits (`null`은 unlimited)](https://openrouter.ai/docs/api_reference/limits)
- [현재 API key metadata](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)
- [응답 usage/cost accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)

## 설치

Python 3.11 이상이 필요합니다. macOS에서는 `python` 대신 `python3`를 사용합니다.

```bash
cd /Users/m4_25/ox-alpha-review-py
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

`.env.example`을 `.env`로 복사해 값을 채운 뒤 권한을 제한합니다.

```bash
chmod 600 .env
```

## Production acceptance

OpenRouter 지원팀의 서면 답변에는 다음 의미가 모두 명시되어야 합니다.

- API key limit `0`은 unlimited이며 결제 차단으로 사용할 수 없음
- 요청의 `provider.max_price=0`이 모든 nonzero price를 실행 전에 막는 hard
  pre-authorization ceiling임
- overage, negative balance, invoice 경로가 없음
- BYOK, 결제수단, purchased credit, auto top-up을 사용하지 않음

`ox-alpha-review-accept`는 위 의미를 확인하기 위해 서면 답변에 `OpenRouter`,
`provider.max_price`, `limit 0`, `unlimited`, `pre-authorization`, `nonzero price`,
`overage`, `negative balance`, `invoice`, `payment method`, `purchased credits`,
`auto top-up`, `BYOK` 용어가 모두 포함되어야만 기록을 생성합니다.

확인 원문은 저장소 밖에 보관하고 아래 명령으로 키/모델/endpoint 상태를 다시 검증한
후 로컬 acceptance record를 만듭니다. 이 명령은 completion 요청을 보내지 않습니다.

```bash
ox-alpha-review-accept --check-only
ox-alpha-review-accept --confirmation-file /absolute/path/openrouter-confirmation.txt
```

acceptance record와 쿼터/latch는 `~/.ox-alpha-review-py/safety.sqlite3`에 0600으로
저장되며 Git에 커밋하지 않습니다.

## 서버 실행

```bash
source .venv/bin/activate
ox-alpha-review
```

또는:

```bash
python -m uvicorn ox_alpha_review.main:app_factory --factory \
  --host 127.0.0.1 --port 8025
```

확인:

```bash
curl -i http://127.0.0.1:8025/healthz
curl -i http://127.0.0.1:8025/readyz
```

GitHub App Webhook URL은 외부 HTTPS 주소의 `/webhook`입니다. 로컬 개발 서버는
GitHub에서 직접 접근할 수 없으므로 HTTPS reverse proxy 또는 안전한 tunnel이
필요합니다.
