# AGENTS.md

## Mission

Build `ox-alpha-review-py` as a self-hosted GitHub App with the same observable PR
review behavior as the pinned Claude/Codex/Gemini reference bots. Ox Alpha is the
only inference model. The system must operate exclusively on free quota and must
never authorize paid inference.

When availability and zero-cost safety conflict, choose safety and refuse service.

## Non-negotiable free-only rules

- Use only the exact model ID `stealth/ox-alpha` through OpenRouter.
- Route only to provider slug `stealth`; set `order: ["stealth"]`,
  `only: ["stealth"]`, `allow_fallbacks: false`, and
  `require_parameters: true`.
- Set every supported `provider.max_price` field to the string `"0"`.
- Use a dedicated Free-tier API key with a verified USD 0 spending limit, no payment
  method, no purchased credits, no auto top-up, and no BYOK configuration.
- Keep production readiness disabled until OpenRouter confirms in writing that the
  USD 0 key limit is a hard pre-authorization ceiling with no overage, negative
  balance, or invoice path. A successful free smoke call is only a compatibility
  check, not proof of billing semantics.
- Never add `openrouter/free`, model aliases, `:free`/`:floor` variants, alternate
  providers, paid fallbacks, account rotation, tools, plugins, server tools, web
  search, caching, or non-default service tiers.
- Validate key state plus exact model, endpoint, provider, base prices, and conditional
  price overrides before every inference request. Unknown state is unsafe.
- Reserve local quota atomically before inference. Count failures, cancellations,
  parse errors, stale-head results, and diff-only fallback calls. Never refund an
  attempt.
- Route every completion POST—including provisioning, latch-clear, and acceptance
  smoke calls—through the same quota-guarded transport. No second HTTP path may own
  the completion endpoint.
- Do not automatically retry inference. HTTP 402, price drift, key-cap drift,
  response identity mismatch, or nonzero response cost must persist a safety latch.
- The webhook process must not contain or receive an OpenRouter management key.
- No environment variable may override the model/provider allowlist or relax a cost
  guard. There is no emergency bypass or reduced-safety mode.

Any change touching these invariants requires tests proving the completion transport
is not called when a guard fails.

## Environment contract

The tracked `.env.example` is the canonical runtime configuration template. A local
`.env` may contain secrets, must have mode `0600`, and must never be committed. Do not
create secret-bearing `scripts/local_review_env.sh` files.

Required operator-supplied values:

- `GITHUB_APP_ID` for the newly created Ox Alpha GitHub App.
- Exactly one of `GITHUB_APP_PRIVATE_KEY_PATH` or `GITHUB_APP_PRIVATE_KEY`; prefer a
  mode-`0600` key file outside the repository.
- `GITHUB_WEBHOOK_SECRET`, newly generated for this App and not reused from another
  review bot.
- `OPENROUTER_API_KEY`, an inference-only key for the dedicated Free-tier account.
  It must not be a management/provisioning key.
- `GITHUB_APP_SLUG` must match the new App for follow-ups and meta replies, unless the
  implementation obtains and verifies the slug from GitHub at startup.

Safe runtime settings may expose only bounded operational values such as
`GITHUB_API_BASE`, `HOST`, `PORT`, `DRY_RUN`, `REPO_CACHE_DIR`, `GIT_TIMEOUT_SEC`,
`FILE_MAX_BYTES`, `DATA_FILE_MAX_BYTES`, `WEBHOOK_MAX_BODY_BYTES`, and
`REVIEW_QUEUE_MAXSIZE`. Version 0.1 must require `REVIEW_CONCURRENCY=1`.

Do not accept environment overrides for the OpenRouter base URL, model, provider,
fallback behavior, price ceilings, service tier, tools/plugins, BYOK, rolling quota
limit, safety latch, acceptance record, or production-readiness decision. Reject any
management key or reserved override rather than silently ignoring it.

## Architecture and dependency direction

Use four layers with dependencies pointing inward:

```text
interfaces (FastAPI, composition root)
    -> application (review orchestration/use cases)
        -> domain (entities, value objects, ports)
infrastructure (GitHub, OpenRouter, Git, SQLite adapters)
    -> domain/application ports
```

- `domain` must not import FastAPI, httpx, SQLite, environment settings, filesystem
  process APIs, or provider SDKs.
- `application` coordinates domain ports and owns workflow policy. It must not build
  raw HTTP requests or execute SQL.
- `infrastructure` implements narrow ports for external systems. Adapters do not own
  review workflow decisions.
- `interfaces` validates transport input and wires dependencies in one composition
  root. Avoid service locators, mutable module globals, and hidden singletons.
- Reuse the provider-neutral `reviewbot_common` package from the pinned Claude
  baseline. Keep Ox Alpha code under `src/ox_alpha_review`.

## SOLID requirements

### Single Responsibility

- Separate key inspection, model catalog inspection, zero-cost policy, quota
  reservation, delivery idempotency, safety latch, completion HTTP transport,
  response parsing, and GitHub posting.
- A class should have one reason to change. Do not create a single provider client
  that validates billing, owns SQLite, builds prompts, parses reviews, and posts them.

### Open/Closed

- Extend behavior through ports and adapters, not conditionals spread through use
  cases.
- Runtime configuration may tune safe limits but must not open extension points for
  alternate models/providers or paid routing.

### Liskov Substitution

- Implementations of a port must preserve its success/error contract and fail-closed
  guarantees. A fake or alternate adapter must not return a weaker notion of
  "zero-cost verified."
- `ReviewEngine` implementations return the shared `ReviewResult` contract and raise
  typed failures expected by the common fallback/notice flow.

### Interface Segregation

- Prefer small protocols such as `KeyMetadataReader`, `ModelCatalogReader`,
  `FreeQuotaLedger`, `DeliveryStore`, `SafetyLatch`, `CompletionTransport`, and
  `ReviewEngine`.
- Do not expose management-key operations through the runtime inference interface.

### Dependency Inversion

- Application use cases depend on protocols, not httpx clients, SQLite connections,
  concrete GitHub clients, or environment variables.
- Inject clocks, ID/hash generation, filesystem roots, and transports so edge cases
  are deterministic in tests.

## Compatibility rules

### Pinned baselines

- Claude executable and golden-test baseline:
  `swift-man/claude-pr-review-py@7de1ac65850b12c7ee82f17710a374c41ad494df`.
- Gemini comparison baseline:
  `swift-man/gemini-pr-review-py@69055fb7922c8c225b2853dea2dd71f43ab60208`.
- Codex comparison baseline:
  `swift-man/codex-pr-review-py@dda9fb8aac97dcecb4a56dfbaceb2db2ef48f89c`.
- Resolve conflicting behavior in this order: explicit rules in this file, behavior
  shared by all three pinned commits, then the pinned Claude baseline. Provider login,
  fallback, account rotation, model labels, and transport details are provider-specific
  and are not compatibility requirements.
- Record any other conflict in `docs/compatibility-matrix.md` and obtain explicit
  approval before implementation. Do not silently combine behaviors.

### Observable behavior

- Preserve full tracked-repository context with changed files first. Do not silently
  switch private repositories to changed-files-only review.
- Keep diff-only mode solely as the existing context-limit fallback.
- Post inline comments only on GitHub-accepted RIGHT-side diff lines; surface other
  findings in the review body using the existing fallback behavior.
- Preserve accepted webhook actions, draft skipping, bounded serialized queue,
  immutable head checkout, stale-head check, Korean review output, review history,
  follow-ups, meta replies, and notice deduplication from the pinned baseline.

## Security and privacy

- Verify webhook HMAC against the raw bounded body before JSON parsing or queueing.
- Treat PR metadata, paths, patches, history, and repository contents as untrusted.
- Reject symlink/path escapes using resolved-root checks before reading file content.
- Never log API keys, GitHub tokens, webhook secrets, PEM data, repository source,
  prompts, completions, or raw reasoning traces.
- Use direct text-only HTTP inference. The model receives no command, filesystem,
  network, GitHub, or tool execution capability.
- Document that the Ox Alpha provider may retain prompts/completions. Do not alter
  the full-context behavior without an explicit product decision.

## Testing and quality gates

- Every bug fix starts with a regression test when practical.
- Unit tests must use injected fakes; live OpenRouter/GitHub calls are opt-in
  acceptance tests only.
- Test every fail-closed branch and assert zero completion HTTP requests were sent.
- Test SQLite concurrency, corruption, permission failure, clock rollback, process
  lock contention, crash-after-send state, delivery redelivery, and 45/46 quota
  boundaries.
- Test exact request snapshots: model/provider IDs, disabled fallback, zero price
  ceilings, omitted paid features, and bounded output/input.
- Test response identity, routing metadata, usage cost, parser bounds, diff-line
  filtering, 422 fallback, stale heads, and secret/log redaction.
- Before handoff, run `pytest`, `ruff check .`, `ruff format --check .`, and strict
  `mypy` using the commands defined by `pyproject.toml`.

## Review scope

`.reviewbot.yml` is authoritative. Dependency trees, environments, caches, generated
outputs, bulky binary/media assets, documentation, lockfiles, and local secret files
are excluded. Security-critical cost-firewall and provisioning files are always
reviewed even if another pattern would exclude them.

## Change and PR discipline

- Keep changes small and cohesive; do not mix provider integration with unrelated
  refactors.
- Preserve user changes in a dirty worktree and stage only intended paths.
- Use conventional commit/PR title types such as `feat:`, `fix:`, `docs:`, or
  `test:`; never prefix titles with `[Codex]`.
- Create Ready PRs, never Draft PRs. After an authorized merge, delete the merged
  branch.
- Never commit secrets, generated credentials, runtime databases, acceptance
  records, repository caches, or local environment files.
