from typing import Protocol


class DeliveryStore(Protocol):
    async def claim(self, delivery_id: str) -> bool:
        """Persist a delivery before queueing; false means it was already observed."""
        ...

    async def abandon(self, delivery_id: str) -> None:
        """Release a delivery that was never queued."""
        ...

    async def finish(self, delivery_id: str) -> None:
        """Persist terminal handling, including failures and cancellation."""
        ...
