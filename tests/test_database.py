"""Tests for the database layer retry logic and lifecycle.

These tests mock asyncpg to verify retry behavior, idempotency handling,
connection lifecycle, and error classification without requiring a real
PostgreSQL instance.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import database


@pytest.fixture(autouse=True)
def reset_database_module():
    """Reset module-level state between tests."""
    database._pool = None
    database._closing = False
    database._last_success_at = None
    database._last_failure = None
    yield
    database._pool = None
    database._closing = False


def _make_pool(*, is_closing=False):
    """Create a mock pool that behaves like asyncpg.Pool."""
    pool = MagicMock()
    pool.is_closing.return_value = is_closing
    pool.get_size.return_value = 5
    pool.get_idle_size.return_value = 3
    pool.get_min_size.return_value = 1
    pool.get_max_size.return_value = 10
    pool.close = AsyncMock()
    pool.terminate = MagicMock()
    return pool


class _AcquireContext:
    """Mimics asyncpg's pool.acquire() async context manager."""

    def __init__(self, conn=None, fail_on_enter=False):
        self._conn = conn or MagicMock()
        self._fail_on_enter = fail_on_enter

    async def __aenter__(self):
        if self._fail_on_enter:
            raise OSError("connection refused")
        return self._conn

    async def __aexit__(self, *args):
        return False


def _setup_pool_acquire(pool, conn=None):
    """Set up pool.acquire() to return a working async context manager."""
    pool.acquire = lambda timeout=None: _AcquireContext(conn)


class TestRetryLogicIdempotent:
    @pytest.mark.asyncio
    async def test_idempotent_retries_on_transient_error(self):
        pool = _make_pool()
        database._pool = pool
        _setup_pool_acquire(pool)

        call_count = 0

        async def operation(conn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("connection reset")
            return [{"id": 1}]

        result = await database._run(operation, idempotent=True)
        assert result == [{"id": 1}]
        assert call_count == 2


class TestRetryLogicNonIdempotent:
    @pytest.mark.asyncio
    async def test_non_idempotent_no_retry_after_execute(self):
        """After acquire succeeds and execution starts, non-idempotent ops must NOT retry."""
        pool = _make_pool()
        database._pool = pool
        _setup_pool_acquire(pool)

        async def operation(conn):
            raise OSError("connection reset during INSERT")

        with pytest.raises(database.DatabaseUnavailable):
            await database._run(operation, idempotent=False)

    @pytest.mark.asyncio
    async def test_non_idempotent_retries_when_acquire_fails(self):
        """If acquire itself fails (before any SQL), non-idempotent ops CAN retry."""
        pool = _make_pool()
        database._pool = pool

        call_count = 0

        def acquire_factory(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _AcquireContext(fail_on_enter=True)
            return _AcquireContext()

        pool.acquire = acquire_factory

        async def operation(conn):
            return 42

        result = await database._run(operation, idempotent=False)
        assert result == 42
        assert call_count == 2


class TestDatabaseUnavailable:
    def test_raises_when_pool_is_none(self):
        database._pool = None
        with pytest.raises(database.DatabaseUnavailable):
            database._get_pool()

    @pytest.mark.asyncio
    async def test_raises_when_closing(self):
        database._closing = True
        database._pool = _make_pool()
        with pytest.raises(database.DatabaseUnavailable, match="shutting down"):
            await database._run(lambda conn: conn.fetch("SELECT 1"), idempotent=True)


class TestPoolStats:
    def test_stats_when_disconnected(self):
        stats = database.pool_stats()
        assert stats["connected"] is False
        assert "size" not in stats

    def test_stats_when_connected(self):
        database._pool = _make_pool()
        database._last_success_at = 1234567890.0
        stats = database.pool_stats()
        assert stats["connected"] is True
        assert stats["size"] == 5
        assert stats["idle"] == 3
        assert stats["last_success_at"] == 1234567890.0
        assert stats["last_failure"] is None


class TestPoolShutdown:
    @pytest.mark.asyncio
    async def test_close_sets_closing_flag(self):
        pool = _make_pool()
        database._pool = pool
        await database.close_database()
        assert database._closing is True
        assert database._pool is None

    @pytest.mark.asyncio
    async def test_close_terminates_on_timeout(self):
        pool = _make_pool()
        pool.close = AsyncMock(side_effect=asyncio.TimeoutError)
        database._pool = pool

        await database.close_database()
        pool.terminate.assert_called_once()
        assert database._pool is None

    @pytest.mark.asyncio
    async def test_close_when_already_none(self):
        database._pool = None
        await database.close_database()


class TestCheckConnection:
    @pytest.mark.asyncio
    async def test_reports_not_connected_when_pool_none(self):
        ok, detail = await database.check_connection()
        assert ok is False
        assert "pool is not open" in detail

    @pytest.mark.asyncio
    async def test_error_detail_does_not_leak_connection_info(self):
        pool = _make_pool()
        database._pool = pool

        with patch.object(database, '_fetch_val', side_effect=OSError("conn refused to postgresql://user:secret@host/db")):
            ok, detail = await database.check_connection()
        assert ok is False
        assert "secret" not in detail
        assert "query failed" in detail


class TestRecordFailure:
    def test_only_stores_class_name(self):
        err = ConnectionRefusedError("postgresql://user:pass@host:5432/db refused")
        database._record_failure(err)
        assert database._last_failure == "ConnectionRefusedError"
        assert "pass" not in database._last_failure


class TestConnectDatabase:
    @pytest.mark.asyncio
    async def test_fatal_config_error_no_retry(self):
        """Wrong password should fail immediately, not retry."""
        import asyncpg

        call_count = 0

        async def mock_new_pool():
            nonlocal call_count
            call_count += 1
            raise asyncpg.exceptions.InvalidPasswordError("password authentication failed")

        with patch.object(database, '_new_pool', side_effect=mock_new_pool):
            with pytest.raises(asyncpg.exceptions.InvalidPasswordError):
                await database.connect_database()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_transient_error_retries_with_max_attempts(self):
        """Connection refused should retry up to DB_CONNECT_MAX_ATTEMPTS."""
        import config
        original = config.DB_CONNECT_MAX_ATTEMPTS
        config.DB_CONNECT_MAX_ATTEMPTS = 3
        original_start = config.DB_CONNECT_BACKOFF_START
        config.DB_CONNECT_BACKOFF_START = 0.01

        call_count = 0

        async def mock_new_pool():
            nonlocal call_count
            call_count += 1
            raise ConnectionRefusedError("connection refused")

        try:
            with patch.object(database, '_new_pool', side_effect=mock_new_pool):
                with pytest.raises(ConnectionRefusedError):
                    await database.connect_database()
            assert call_count == 3
        finally:
            config.DB_CONNECT_MAX_ATTEMPTS = original
            config.DB_CONNECT_BACKOFF_START = original_start


class TestIsConnected:
    def test_false_when_no_pool(self):
        assert database.is_connected() is False

    def test_true_when_pool_active(self):
        database._pool = _make_pool()
        assert database.is_connected() is True

    def test_false_when_pool_closing(self):
        database._pool = _make_pool(is_closing=True)
        assert database.is_connected() is False
