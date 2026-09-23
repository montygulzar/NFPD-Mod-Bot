"""Tests for health state transitions and endpoint behavior."""
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from health import HealthServer, _latency_ms


class FakeBot:
    def __init__(self, *, ready=True, closed=False, latency=0.05, guilds=None):
        self._ready = ready
        self._closed = closed
        self._latency = latency
        self.guilds = guilds or []

    def is_ready(self):
        return self._ready

    def is_closed(self):
        return self._closed

    @property
    def latency(self):
        return self._latency


class TestHealthStateTransitions:
    def test_initial_state_not_connected(self):
        server = HealthServer(FakeBot(ready=False))
        ok, detail = server._discord_state()
        assert ok is False
        assert "connecting" in detail

    def test_connected_state(self):
        server = HealthServer(FakeBot())
        server.note_connected()
        ok, detail = server._discord_state()
        assert ok is True
        assert "connected" in detail

    def test_disconnected_within_grace(self):
        server = HealthServer(FakeBot())
        server.note_connected()
        server.note_disconnected()
        ok, detail = server._discord_state()
        assert ok is True
        assert "reconnecting" in detail

    def test_disconnected_beyond_grace(self):
        server = HealthServer(FakeBot())
        server.note_connected()
        server._disconnected_since = time.monotonic() - 200
        ok, detail = server._discord_state()
        assert ok is False
        assert "disconnected" in detail

    def test_reconnect_clears_disconnected(self):
        server = HealthServer(FakeBot())
        server.note_disconnected()
        server.note_connected()
        ok, detail = server._discord_state()
        assert ok is True
        assert "connected" in detail

    def test_closed_bot(self):
        server = HealthServer(FakeBot(closed=True))
        ok, detail = server._discord_state()
        assert ok is False
        assert "closed" in detail

    def test_multiple_disconnect_calls_idempotent(self):
        server = HealthServer(FakeBot())
        server.note_disconnected()
        first_time = server._disconnected_since
        server.note_disconnected()
        assert server._disconnected_since == first_time


class TestLatencyMs:
    def test_normal_latency(self):
        assert _latency_ms(FakeBot(latency=0.05)) == 50.0

    def test_nan_latency(self):
        assert _latency_ms(FakeBot(latency=float("nan"))) is None

    def test_inf_latency(self):
        assert _latency_ms(FakeBot(latency=float("inf"))) is None
