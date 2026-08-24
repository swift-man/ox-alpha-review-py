import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ox_alpha_review.application.ports import QuotaExceededError, StateSafetyError
from ox_alpha_review.infrastructure.sqlite_state import (
    SQLiteCompletionAttemptJournal,
    SQLiteDeliveryStore,
    SQLiteFreeQuotaLedger,
    SQLiteProductionReadiness,
    SQLiteSafetyDatabase,
    SQLiteSafetyLatch,
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


async def _database(tmp_path: Path) -> SQLiteSafetyDatabase:
    database = SQLiteSafetyDatabase(tmp_path / "state" / "safety.sqlite3")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_quota_allows_45_and_refuses_46(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    ledger = SQLiteFreeQuotaLedger(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)

    for number in range(45):
        await ledger.reserve(str(number), now)

    with pytest.raises(QuotaExceededError):
        await ledger.reserve("46", now)


@pytest.mark.asyncio
async def test_quota_survives_restart_and_expires_at_exactly_24_hours(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    start = datetime(2026, 8, 24, tzinfo=UTC)
    await SQLiteFreeQuotaLedger(database).reserve("first", start)

    restarted = SQLiteSafetyDatabase(database.path)
    await restarted.initialize()
    remaining = await SQLiteFreeQuotaLedger(restarted).reserve(
        "second",
        start + timedelta(hours=24),
    )

    assert remaining == 44


@pytest.mark.asyncio
async def test_unverified_completion_attempt_survives_restart_and_fails_closed(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    journal = SQLiteCompletionAttemptJournal(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await journal.begin("sent-before-crash", now)

    restarted = SQLiteCompletionAttemptJournal(SQLiteSafetyDatabase(database.path))
    with pytest.raises(StateSafetyError, match="unverified completion attempt"):
        await restarted.assert_clean()


@pytest.mark.asyncio
async def test_verified_completion_attempt_is_removed_durably(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    journal = SQLiteCompletionAttemptJournal(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await journal.begin("verified", now)
    await journal.verify("verified")

    await SQLiteCompletionAttemptJournal(SQLiteSafetyDatabase(database.path)).assert_clean()


@pytest.mark.asyncio
async def test_quota_clock_rollback_fails_closed(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    ledger = SQLiteFreeQuotaLedger(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await ledger.reserve("first", now)

    with pytest.raises(StateSafetyError, match="backwards"):
        await ledger.reserve("second", now - timedelta(microseconds=1))


@pytest.mark.asyncio
async def test_quota_rejects_naive_clock_before_writing(tmp_path: Path) -> None:
    database = await _database(tmp_path)

    with pytest.raises(StateSafetyError, match="naive clock"):
        await SQLiteFreeQuotaLedger(database).reserve("naive", datetime(2026, 8, 24))


@pytest.mark.asyncio
async def test_duplicate_reservation_id_fails_closed_without_extra_attempt(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    ledger = SQLiteFreeQuotaLedger(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    await ledger.reserve("duplicate", now)

    with pytest.raises(StateSafetyError, match="duplicate quota reservation"):
        await ledger.reserve("duplicate", now)


@pytest.mark.asyncio
async def test_quota_corrupt_clock_state_fails_closed(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    connection = database.connect()
    connection.execute("INSERT INTO safety_meta(key, value) VALUES('last_clock_us', 'broken')")
    connection.close()

    with pytest.raises(StateSafetyError, match="corrupt"):
        await SQLiteFreeQuotaLedger(database).reserve(
            "first",
            datetime(2026, 8, 24, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_corrupt_database_fails_initialization(tmp_path: Path) -> None:
    path = tmp_path / "state" / "safety.sqlite3"
    path.parent.mkdir()
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(StateSafetyError, match="database"):
        await SQLiteSafetyDatabase(path).initialize()


@pytest.mark.asyncio
async def test_database_permission_failure_fails_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = SQLiteSafetyDatabase(tmp_path / "state" / "safety.sqlite3")

    def deny_permissions(path: object, mode: int) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(os, "chmod", deny_permissions)
    with pytest.raises(StateSafetyError, match="initialization failed"):
        await database.initialize()


@pytest.mark.asyncio
async def test_database_lock_contention_fails_before_reservation(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    blocker = database.connect()
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StateSafetyError, match="failed closed"):
            await SQLiteFreeQuotaLedger(database).reserve(
                "locked",
                datetime(2026, 8, 24, tzinfo=UTC),
            )
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.asyncio
async def test_concurrent_46_reservations_never_exceed_45(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    ledger = SQLiteFreeQuotaLedger(database)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    results = await asyncio.gather(
        *(ledger.reserve(str(number), now) for number in range(46)),
        return_exceptions=True,
    )

    assert sum(isinstance(result, int) for result in results) == 45
    assert sum(isinstance(result, QuotaExceededError) for result in results) == 1


@pytest.mark.asyncio
async def test_safety_database_permissions_and_latch_persist(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    assert os.stat(database.path).st_mode & 0o777 == 0o600
    latch = SQLiteSafetyLatch(database)
    await latch.trip("price drift", datetime(2026, 8, 24, tzinfo=UTC))

    with pytest.raises(StateSafetyError, match="price drift"):
        await SQLiteSafetyLatch(SQLiteSafetyDatabase(database.path)).assert_clear()


@pytest.mark.asyncio
async def test_latch_persistence_failure_keeps_process_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = await _database(tmp_path)
    latch = SQLiteSafetyLatch(database)

    with monkeypatch.context() as patch:
        patch.setattr(
            database,
            "connect",
            lambda: (_ for _ in ()).throw(StateSafetyError("disk unavailable")),
        )
        with pytest.raises(StateSafetyError, match="process remains fail-closed"):
            await latch.trip("price drift", datetime(2026, 8, 24, tzinfo=UTC))

    with pytest.raises(StateSafetyError, match="process remains fail-closed"):
        await latch.assert_clear()


@pytest.mark.asyncio
async def test_readiness_is_bound_to_key_fingerprint(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    readiness = SQLiteProductionReadiness(database)
    assert await readiness.status("key-a") == (
        False,
        "written max-price billing confirmation has not been accepted",
    )
    await readiness.record(
        key_fingerprint="key-a",
        confirmation_sha256="a" * 64,
        accepted_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert await readiness.status("key-a") == (True, "ready")
    assert (await readiness.status("key-b"))[0] is False


@pytest.mark.asyncio
async def test_legacy_acceptance_is_migrated_but_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "state" / "safety.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE production_acceptance (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            key_fingerprint TEXT NOT NULL,
            confirmation_sha256 TEXT NOT NULL,
            accepted_at_us INTEGER NOT NULL
        );
        INSERT INTO production_acceptance(
            singleton, key_fingerprint, confirmation_sha256, accepted_at_us
        ) VALUES(1, 'key-a', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1);
        """
    )
    connection.close()

    database = SQLiteSafetyDatabase(path)
    await database.initialize()
    readiness = SQLiteProductionReadiness(database)

    assert await readiness.status("key-a") == (
        False,
        "production acceptance uses an outdated billing policy",
    )


@pytest.mark.asyncio
async def test_delivery_store_claims_once_and_queue_rejection_can_release(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    store = SQLiteDeliveryStore(
        database,
        _Clock(datetime(2026, 8, 24, tzinfo=UTC)),
    )
    assert await store.claim("delivery") is True
    assert await store.claim("delivery") is False
    await store.abandon("delivery")
    assert await store.claim("delivery") is True
    await store.finish("delivery")
    assert await store.claim("delivery") is False


@pytest.mark.asyncio
async def test_delivery_claim_survives_crash_before_finish_and_blocks_redelivery(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    assert await SQLiteDeliveryStore(database, _Clock(now)).claim("sent-before-crash") is True

    restarted = SQLiteDeliveryStore(SQLiteSafetyDatabase(database.path), _Clock(now))
    assert await restarted.claim("sent-before-crash") is False
