import asyncio

import pytest

from reviewbot_common.application.webhook_handler import WebhookHandler


def _handler() -> WebhookHandler:
    return WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
    )


def _payload(*, action: str, draft: bool) -> dict[str, object]:
    return {
        "action": action,
        "pull_request": {"number": 1, "draft": draft},
        "repository": {"full_name": "owner/repo"},
        "installation": {"id": 123},
    }


class _DeliveryStore:
    def __init__(self, finish_error: Exception | None = None) -> None:
        self.claimed: set[str] = set()
        self.abandoned: list[str] = []
        self.finish_error = finish_error

    async def claim(self, delivery_id: str) -> bool:
        if delivery_id in self.claimed:
            return False
        self.claimed.add(delivery_id)
        return True

    async def abandon(self, delivery_id: str) -> None:
        self.claimed.discard(delivery_id)
        self.abandoned.append(delivery_id)

    async def finish(self, delivery_id: str) -> None:
        if self.finish_error is not None:
            raise self.finish_error


@pytest.mark.asyncio
async def test_accept_skips_draft_pr_before_ready_for_review() -> None:
    status, reason = await _handler().accept(
        "pull_request",
        "delivery-1",
        _payload(action="opened", draft=True),
    )

    assert (status, reason) == (202, "skipped-draft")


@pytest.mark.asyncio
async def test_accept_queues_ready_for_review_even_if_payload_still_says_draft() -> None:
    status, reason = await _handler().accept(
        "pull_request",
        "delivery-1",
        _payload(action="ready_for_review", draft=True),
    )

    assert (status, reason) == (202, "queued")


@pytest.mark.asyncio
async def test_accept_rejects_pull_request_while_stopping() -> None:
    handler = _handler()
    handler._stopping = True  # noqa: SLF001

    status, reason = await handler.accept(
        "pull_request",
        "delivery-1",
        _payload(action="opened", draft=False),
    )

    assert (status, reason) == (503, "shutting-down")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _payload(action="opened", draft=False) | {"pull_request": {"number": True}},
        _payload(action="opened", draft=False) | {"installation": {"id": "NaN"}},
        _payload(action="opened", draft=False) | {"repository": {"full_name": "invalid"}},
    ],
)
async def test_accept_rejects_invalid_identifiers(payload: dict[str, object]) -> None:
    assert await _handler().accept("pull_request", "delivery-1", payload) == (
        400,
        "invalid-payload",
    )


@pytest.mark.asyncio
async def test_duplicate_delivery_is_ignored_before_queueing_twice() -> None:
    deliveries = _DeliveryStore()
    handler = WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
        delivery_store=deliveries,
    )
    payload = _payload(action="opened", draft=False)

    assert await handler.accept("pull_request", "delivery-1", payload) == (202, "queued")
    assert await handler.accept("pull_request", "delivery-1", payload) == (202, "duplicate")
    assert handler._queue.qsize() == 1  # noqa: SLF001
    await handler.stop()


@pytest.mark.asyncio
async def test_queue_full_releases_rejected_delivery_claim() -> None:
    deliveries = _DeliveryStore()
    handler = WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
        queue_maxsize=1,
        delivery_store=deliveries,
    )
    payload = _payload(action="opened", draft=False)

    assert await handler.accept("pull_request", "delivery-1", payload) == (202, "queued")
    assert await handler.accept("pull_request", "delivery-2", payload) == (503, "queue-full")
    assert deliveries.abandoned == ["delivery-2"]
    await handler.stop()


@pytest.mark.asyncio
async def test_shutdown_releases_delivery_that_was_dropped_before_processing() -> None:
    deliveries = _DeliveryStore()
    handler = WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
        delivery_store=deliveries,
    )
    assert (
        await handler.accept(
            "pull_request",
            "delivery-1",
            _payload(action="opened", draft=False),
        )
    ) == (202, "queued")

    await handler.stop()

    assert deliveries.abandoned == ["delivery-1"]
    assert await deliveries.claim("delivery-1") is True


@pytest.mark.asyncio
async def test_delivery_finish_failure_stops_worker_and_rejects_new_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deliveries = _DeliveryStore(RuntimeError("disk unavailable"))
    handler = WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
        delivery_store=deliveries,
    )

    process_started = asyncio.Event()
    release_process = asyncio.Event()

    async def process(_: object) -> None:
        process_started.set()
        await release_process.wait()

    monkeypatch.setattr(handler, "_process", process)
    await handler.start()
    assert (
        await handler.accept(
            "pull_request",
            "delivery-1",
            _payload(action="opened", draft=False),
        )
    ) == (202, "queued")
    await process_started.wait()
    assert (
        await handler.accept(
            "pull_request",
            "delivery-queued",
            _payload(action="opened", draft=False),
        )
    ) == (202, "queued")
    release_process.set()
    await handler._queue.join()  # noqa: SLF001

    assert handler.health_status() == (False, "delivery completion persistence failed")
    assert "delivery-queued" in deliveries.abandoned
    assert (
        await handler.accept(
            "pull_request",
            "delivery-2",
            _payload(action="opened", draft=False),
        )
    ) == (503, "worker-unhealthy")
    await handler.stop()


@pytest.mark.asyncio
async def test_health_is_rechecked_after_delivery_claim_await() -> None:
    handler: WebhookHandler

    class _RacingDeliveryStore(_DeliveryStore):
        async def claim(self, delivery_id: str) -> bool:
            claimed = await super().claim(delivery_id)
            handler._fatal_error = "delivery completion persistence failed"  # noqa: SLF001
            handler._stopping = True  # noqa: SLF001
            return claimed

    deliveries = _RacingDeliveryStore()
    handler = WebhookHandler(
        secret="secret",
        github=object(),  # type: ignore[arg-type]
        use_case=object(),  # type: ignore[arg-type]
        delivery_store=deliveries,
    )

    assert (
        await handler.accept(
            "pull_request",
            "delivery-race",
            _payload(action="opened", draft=False),
        )
    ) == (503, "worker-unhealthy")
    assert deliveries.abandoned == ["delivery-race"]
    assert handler._queue.empty()  # noqa: SLF001
