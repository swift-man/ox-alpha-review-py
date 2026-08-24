from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ox_alpha_review.application.ports import (
    Clock,
    QuotaExceededError,
    ReadinessError,
    SafetyLatchTrippedError,
    StateSafetyError,
)

_DAY = timedelta(hours=24)
_QUOTA_LIMIT = 45
_ACCEPTANCE_POLICY_VERSION = 2
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SQLiteSafetyDatabase:
    """Creates and opens the fail-closed local safety database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=0.25,
                isolation_level=None,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 250")
            return connection
        except sqlite3.Error as exc:
            raise StateSafetyError("safety database cannot be opened") from exc

    def _initialize_sync(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            connection = self.connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS quota_reservations (
                        reservation_id TEXT PRIMARY KEY,
                        reserved_at_us INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS quota_reserved_at
                        ON quota_reservations(reserved_at_us);
                    CREATE TABLE IF NOT EXISTS completion_attempts (
                        reservation_id TEXT PRIMARY KEY,
                        started_at_us INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS safety_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS safety_latch (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        reason TEXT NOT NULL,
                        tripped_at_us INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS production_acceptance (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        key_fingerprint TEXT NOT NULL,
                        confirmation_sha256 TEXT NOT NULL,
                        accepted_at_us INTEGER NOT NULL,
                        policy_version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS webhook_deliveries (
                        delivery_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        accepted_at_us INTEGER NOT NULL,
                        finished_at_us INTEGER
                    );
                    """
                )
                acceptance_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(production_acceptance)")
                }
                if "policy_version" not in acceptance_columns:
                    connection.execute(
                        "ALTER TABLE production_acceptance "
                        "ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1"
                    )
                result = connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise StateSafetyError("safety database integrity check failed")
            finally:
                connection.close()
            os.chmod(self.path, 0o600)
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode != 0o600:
                raise StateSafetyError("safety database mode is not 0600")
        except StateSafetyError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise StateSafetyError("safety database initialization failed") from exc


class SQLiteFreeQuotaLedger:
    def __init__(self, database: SQLiteSafetyDatabase) -> None:
        self._database = database

    async def reserve(self, reservation_id: str, now: datetime) -> int:
        return await asyncio.to_thread(self._reserve_sync, reservation_id, now)

    def _reserve_sync(self, reservation_id: str, now: datetime) -> int:
        now_us = _utc_microseconds(now)
        cutoff_us = _utc_microseconds(now.astimezone(UTC) - _DAY)
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM safety_meta WHERE key = 'last_clock_us'"
            ).fetchone()
            if row is not None:
                try:
                    last_clock_us = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise StateSafetyError("quota clock state is corrupt") from exc
                if now_us < last_clock_us:
                    raise StateSafetyError("system clock moved backwards")

            connection.execute(
                """
                INSERT INTO safety_meta(key, value) VALUES('last_clock_us', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(now_us),),
            )
            connection.execute(
                "DELETE FROM quota_reservations WHERE reserved_at_us <= ?",
                (cutoff_us,),
            )
            count_row = connection.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE reserved_at_us > ?",
                (cutoff_us,),
            ).fetchone()
            if count_row is None or not isinstance(count_row[0], int):
                raise StateSafetyError("quota count state is corrupt")
            count = count_row[0]
            if count >= _QUOTA_LIMIT:
                connection.commit()
                raise QuotaExceededError("free-only rolling quota exhausted")
            try:
                connection.execute(
                    """
                    INSERT INTO quota_reservations(reservation_id, reserved_at_us)
                    VALUES(?, ?)
                    """,
                    (reservation_id, now_us),
                )
            except sqlite3.IntegrityError as exc:
                raise StateSafetyError("duplicate quota reservation id") from exc
            connection.commit()
            return _QUOTA_LIMIT - count - 1
        except QuotaExceededError:
            raise
        except StateSafetyError:
            _rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("quota reservation failed closed") from exc
        finally:
            connection.close()


class SQLiteSafetyLatch:
    def __init__(self, database: SQLiteSafetyDatabase) -> None:
        self._database = database
        self._terminal_error: StateSafetyError | None = None

    async def assert_clear(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        try:
            await asyncio.to_thread(self._assert_clear_sync)
        except SafetyLatchTrippedError:
            raise
        except StateSafetyError as exc:
            terminal = StateSafetyError("safety latch state is unreadable; process is fail-closed")
            self._terminal_error = terminal
            raise terminal from exc

    async def trip(self, reason: str, now: datetime) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        try:
            await asyncio.to_thread(self._trip_sync, reason, now)
        except StateSafetyError as exc:
            terminal = StateSafetyError(
                "safety latch persistence failed; process remains fail-closed"
            )
            self._terminal_error = terminal
            raise terminal from exc

    def _assert_clear_sync(self) -> None:
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT reason FROM safety_latch WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateSafetyError("safety latch cannot be read") from exc
        finally:
            connection.close()
        if row is not None:
            raise SafetyLatchTrippedError(f"safety latch is tripped: {row[0]}")

    def _trip_sync(self, reason: str, now: datetime) -> None:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO safety_latch(singleton, reason, tripped_at_us)
                VALUES(1, ?, ?)
                """,
                (reason[:500], _utc_microseconds(now)),
            )
            connection.commit()
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("safety latch could not be persisted") from exc
        finally:
            connection.close()


class SQLiteCompletionAttemptJournal:
    """Durable proof that every sent completion received a verified zero-cost response."""

    def __init__(self, database: SQLiteSafetyDatabase) -> None:
        self._database = database

    async def assert_clean(self) -> None:
        await asyncio.to_thread(self._assert_clean_sync)

    async def begin(self, reservation_id: str, now: datetime) -> None:
        await asyncio.to_thread(self._begin_sync, reservation_id, now)

    async def verify(self, reservation_id: str) -> None:
        await asyncio.to_thread(self._verify_sync, reservation_id)

    def _assert_clean_sync(self) -> None:
        connection = self._database.connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM completion_attempts").fetchone()
        except sqlite3.Error as exc:
            raise StateSafetyError("completion attempt journal cannot be read") from exc
        finally:
            connection.close()
        if row is None or not isinstance(row[0], int):
            raise StateSafetyError("completion attempt journal is corrupt")
        if row[0] != 0:
            raise StateSafetyError("an unverified completion attempt keeps inference disabled")

    def _begin_sync(self, reservation_id: str, now: datetime) -> None:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT COUNT(*) FROM completion_attempts").fetchone()
            if existing is None or not isinstance(existing[0], int) or existing[0] != 0:
                raise StateSafetyError("completion attempt journal is not clean")
            connection.execute(
                """
                INSERT INTO completion_attempts(reservation_id, started_at_us)
                VALUES(?, ?)
                """,
                (reservation_id, _utc_microseconds(now)),
            )
            connection.commit()
        except StateSafetyError:
            _rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("completion attempt could not be persisted") from exc
        finally:
            connection.close()

    def _verify_sync(self, reservation_id: str) -> None:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM completion_attempts WHERE reservation_id = ?",
                (reservation_id,),
            )
            if cursor.rowcount != 1:
                raise StateSafetyError("completion attempt verification state is missing")
            connection.commit()
        except StateSafetyError:
            _rollback_quietly(connection)
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("completion attempt could not be verified") from exc
        finally:
            connection.close()


class SQLiteProductionReadiness:
    def __init__(self, database: SQLiteSafetyDatabase) -> None:
        self._database = database

    async def assert_ready(self, key_fingerprint: str) -> None:
        ready, reason = await self.status(key_fingerprint)
        if not ready:
            raise ReadinessError(reason)

    async def status(self, key_fingerprint: str) -> tuple[bool, str]:
        return await asyncio.to_thread(self._status_sync, key_fingerprint)

    async def record(
        self,
        *,
        key_fingerprint: str,
        confirmation_sha256: str,
        accepted_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._record_sync,
            key_fingerprint,
            confirmation_sha256,
            accepted_at,
        )

    def _status_sync(self, key_fingerprint: str) -> tuple[bool, str]:
        connection = self._database.connect()
        try:
            row = connection.execute(
                """
                SELECT key_fingerprint, confirmation_sha256, policy_version
                FROM production_acceptance WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateSafetyError("production acceptance cannot be read") from exc
        finally:
            connection.close()
        if row is None:
            return False, "written max-price billing confirmation has not been accepted"
        if row[0] != key_fingerprint:
            return False, "production acceptance belongs to a different OpenRouter key"
        if not isinstance(row[1], str) or len(row[1]) != 64:
            raise StateSafetyError("production acceptance record is corrupt")
        if row[2] != _ACCEPTANCE_POLICY_VERSION:
            return False, "production acceptance uses an outdated billing policy"
        return True, "ready"

    def _record_sync(
        self,
        key_fingerprint: str,
        confirmation_sha256: str,
        accepted_at: datetime,
    ) -> None:
        if len(confirmation_sha256) != 64:
            raise ValueError("confirmation digest must be SHA-256")
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO production_acceptance(
                    singleton, key_fingerprint, confirmation_sha256, accepted_at_us,
                    policy_version
                ) VALUES(1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    key_fingerprint = excluded.key_fingerprint,
                    confirmation_sha256 = excluded.confirmation_sha256,
                    accepted_at_us = excluded.accepted_at_us,
                    policy_version = excluded.policy_version
                """,
                (
                    key_fingerprint,
                    confirmation_sha256,
                    _utc_microseconds(accepted_at),
                    _ACCEPTANCE_POLICY_VERSION,
                ),
            )
            connection.commit()
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("production acceptance could not be recorded") from exc
        finally:
            connection.close()


class SQLiteDeliveryStore:
    def __init__(self, database: SQLiteSafetyDatabase, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def claim(self, delivery_id: str) -> bool:
        return await asyncio.to_thread(self._claim_sync, delivery_id)

    async def abandon(self, delivery_id: str) -> None:
        await asyncio.to_thread(self._abandon_sync, delivery_id)

    async def finish(self, delivery_id: str) -> None:
        await asyncio.to_thread(self._finish_sync, delivery_id)

    def _claim_sync(self, delivery_id: str) -> bool:
        if not delivery_id or len(delivery_id) > 200:
            raise StateSafetyError("GitHub delivery id is invalid")
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO webhook_deliveries(delivery_id, state, accepted_at_us)
                VALUES(?, 'accepted', ?)
                """,
                (delivery_id, _utc_microseconds(self._clock.now())),
            )
            connection.commit()
            return True
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise StateSafetyError("delivery claim failed closed") from exc
        finally:
            connection.close()

    def _abandon_sync(self, delivery_id: str) -> None:
        connection = self._database.connect()
        try:
            connection.execute(
                """
                DELETE FROM webhook_deliveries
                WHERE delivery_id = ? AND state = 'accepted'
                """,
                (delivery_id,),
            )
        except sqlite3.Error as exc:
            raise StateSafetyError("delivery release failed closed") from exc
        finally:
            connection.close()

    def _finish_sync(self, delivery_id: str) -> None:
        connection = self._database.connect()
        try:
            connection.execute(
                """
                UPDATE webhook_deliveries
                SET state = 'finished', finished_at_us = ?
                WHERE delivery_id = ?
                """,
                (_utc_microseconds(self._clock.now()), delivery_id),
            )
        except sqlite3.Error as exc:
            raise StateSafetyError("delivery completion could not be persisted") from exc
        finally:
            connection.close()


def _utc_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StateSafetyError("naive clock value is unsafe")
    utc_value = value.astimezone(UTC)
    delta = utc_value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):
        connection.rollback()
