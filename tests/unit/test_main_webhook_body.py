import pytest

from ox_alpha_review.main import RequestBodyTooLarge, _read_limited_body


class _FakeRequest:
    def __init__(self, chunks: tuple[bytes, ...], headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_read_limited_body_accepts_body_within_limit() -> None:
    body = await _read_limited_body(_FakeRequest((b"ab", b"cd")), 4)  # type: ignore[arg-type]

    assert body == b"abcd"


@pytest.mark.asyncio
async def test_read_limited_body_rejects_stream_over_limit() -> None:
    with pytest.raises(RequestBodyTooLarge):
        await _read_limited_body(_FakeRequest((b"ab", b"cd")), 3)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_read_limited_body_rejects_declared_length_over_limit() -> None:
    request = _FakeRequest((b"",), headers={"Content-Length": "5"})

    with pytest.raises(RequestBodyTooLarge):
        await _read_limited_body(request, 4)  # type: ignore[arg-type]
