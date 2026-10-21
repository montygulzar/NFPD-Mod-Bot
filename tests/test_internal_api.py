"""Tests for POST /internal/unban (used by the NFPD Appeals Bot).

Runs the real handler on a local aiohttp server; Discord and the database are
faked.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
import pytest_asyncio
from aiohttp import web

import config
import internal_api

TOKEN = "test-shared-secret"
MAIN_GUILD = 555


def _http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


class FakeGuild:
    def __init__(self, unban_exc=None):
        self.id = MAIN_GUILD
        self.unban_calls = []
        self._exc = unban_exc

    async def unban(self, user, *, reason=None):
        self.unban_calls.append((user.id, reason))
        if self._exc:
            raise self._exc


class FakeBot:
    def __init__(self, guild):
        self._guild = guild
        self.user = SimpleNamespace(id=1, mention="<@1>", __str__=lambda: "modbot")

    def get_guild(self, guild_id):
        return self._guild if guild_id == MAIN_GUILD else None

    def get_user(self, user_id):
        return None

    async def fetch_user(self, user_id):
        raise _http_error(discord.NotFound, 404)


@pytest_asyncio.fixture
async def api(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_TOKEN", TOKEN)
    monkeypatch.setattr(config, "APPROVED_GUILD_IDS", {MAIN_GUILD})
    monkeypatch.setattr(internal_api, "add_case", AsyncMock(return_value=42))
    monkeypatch.setattr(internal_api, "remove_temp_ban", AsyncMock())
    monkeypatch.setattr(internal_api, "post_to_log_channel", AsyncMock())

    state = SimpleNamespace(guild=FakeGuild())
    app = web.Application()

    async def handler(request):
        return await internal_api.UnbanHandler(FakeBot(state.guild))(request)

    app.router.add_post("/internal/unban", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def call(body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        payload = body if body is not None else {
            "guild_id": MAIN_GUILD, "user_id": 42, "moderator_id": 7,
            "appeal_id": 3, "reason": "Ban appeal #3 accepted",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{port}/internal/unban", json=payload, headers=headers) as r:
                return r.status, await r.json()

    state.call = call
    yield state
    await runner.cleanup()


@pytest.mark.asyncio
async def test_unban_success_records_case(api):
    status, body = await api.call()
    assert status == 200 and body == {"status": "unbanned", "case_id": 42}
    assert api.guild.unban_calls[0][0] == 42
    internal_api.add_case.assert_awaited_once()
    internal_api.remove_temp_ban.assert_awaited_once_with(MAIN_GUILD, 42)


@pytest.mark.asyncio
async def test_wrong_token_is_rejected_and_nothing_happens(api):
    status, body = await api.call(token="nope")
    assert status == 401 and body["status"] == "unauthorized"
    assert api.guild.unban_calls == []


@pytest.mark.asyncio
async def test_missing_token_is_rejected(api):
    status, _ = await api.call(token=None)
    assert status == 401


@pytest.mark.asyncio
async def test_endpoint_disabled_without_configured_token(api, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_TOKEN", "")
    status, body = await api.call(token="")
    assert status == 503 and body["status"] == "disabled"
    assert api.guild.unban_calls == []


@pytest.mark.asyncio
async def test_not_banned_is_success_like(api):
    api.guild = FakeGuild(unban_exc=_http_error(discord.NotFound, 404))
    status, body = await api.call()
    assert status == 200 and body["status"] == "not_banned"
    internal_api.add_case.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_ban_permission(api):
    api.guild = FakeGuild(unban_exc=_http_error(discord.Forbidden, 403))
    status, body = await api.call()
    assert status == 403 and body["status"] == "forbidden"


@pytest.mark.asyncio
async def test_unapproved_guild_refused(api):
    status, body = await api.call({"guild_id": 999, "user_id": 42, "moderator_id": 7})
    assert status == 403 and body["status"] == "forbidden"
    assert api.guild.unban_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"guild_id": MAIN_GUILD, "user_id": "42", "moderator_id": 7},
    {"guild_id": MAIN_GUILD, "moderator_id": 7},
    {"guild_id": MAIN_GUILD, "user_id": True, "moderator_id": 7},
    ["not", "an", "object"],
])
async def test_malformed_body_rejected(api, body):
    status, resp = await api.call(body)
    assert status == 400 and resp["status"] == "bad_request"
    assert api.guild.unban_calls == []


@pytest.mark.asyncio
async def test_case_logging_failure_still_reports_unbanned(api, monkeypatch):
    # The ban is already lifted; a mod-log failure must not make the Appeals
    # Bot think the unban failed.
    monkeypatch.setattr(internal_api, "add_case", AsyncMock(side_effect=RuntimeError("db down")))
    status, body = await api.call()
    assert status == 200 and body["status"] == "unbanned" and body["case_id"] is None
