"""Audit-log lookups are the expensive part of server logging.

They were being spent before anything checked whether the guild had a log
channel at all, and on_member_update fetched the same entry once per branch.
These pin the call counts so neither comes back.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cogs.server_logs as sl


class Role:
    def __init__(self, i):
        self.id, self.mention = i, f"<@&{i}>"
    def __eq__(self, o):
        return isinstance(o, Role) and self.id == o.id
    def __hash__(self):
        return self.id


EVERYONE = Role(0)


class Guild:
    id, name, default_role = 1, "g", EVERYONE
    def __init__(self):
        self.me = MagicMock()
        self.me.guild_permissions.view_audit_log = True


class Member:
    bot = False
    def __init__(self, nick=None, roles=(), timed_out=None):
        self.id, self.guild, self.nick = 5, Guild(), nick
        self.roles = [Role(r) for r in roles]
        self.timed_out_until, self.mention = timed_out, "<@5>"
        self.display_avatar = MagicMock()
        self.display_avatar.url = "u"
    def __str__(self):
        return "U"


LOGGING_OFF = {"server_log_channel_id": None, "log_channel_id": None}
LOGGING_ON = {"server_log_channel_id": 123, "log_channel_id": None}


async def _member_update(settings, before, after):
    """Returns (audit actions requested, posts made)."""
    actions, posts = [], []

    async def fake_audit(guild, target_id, action):
        actions.append(action)
        return None, None

    async def fake_post(guild, embed):
        posts.append(embed)

    cog = sl.ServerLogs(MagicMock())
    with patch.object(sl, "_get_audit_entry", fake_audit), \
         patch.object(sl, "get_guild_settings", AsyncMock(return_value=settings)), \
         patch.object(sl, "post_to_server_log_channel", fake_post):
        await cog.on_member_update(before, after)
    return actions, posts


@pytest.mark.asyncio
async def test_no_audit_lookup_when_the_guild_has_no_log_channel():
    actions, posts = await _member_update(
        LOGGING_OFF, Member(nick="old", roles=[1]), Member(nick="new", roles=[1, 2])
    )
    assert actions == [], "audit log must not be queried with nowhere to post"
    assert posts == []


@pytest.mark.asyncio
async def test_one_member_update_lookup_however_many_fields_changed():
    """Nickname and timeout share a single member_update entry; roles use their
    own action. Four separate lookups for one event is what this prevents."""
    import discord

    actions, posts = await _member_update(
        LOGGING_ON, Member(nick="old", roles=[1]), Member(nick="new", roles=[1, 2])
    )
    assert actions.count(discord.AuditLogAction.member_update) == 1
    assert actions.count(discord.AuditLogAction.member_role_update) == 1
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_nothing_logged_means_nothing_fetched():
    actions, posts = await _member_update(
        LOGGING_ON, Member(nick="same", roles=[1]), Member(nick="same", roles=[1])
    )
    assert actions == []
    assert posts == []


@pytest.mark.asyncio
async def test_guild_settings_are_cached_between_reads():
    """Settings are read on nearly every event, so the row must not be fetched
    each time."""
    import database

    database.clear_settings_cache()
    calls = []

    async def fake_fetch(query, *args):
        calls.append(args)
        return None

    with patch.object(database, "_fetch_one", fake_fetch):
        first = await database.get_guild_settings(42)
        second = await database.get_guild_settings(42)

    assert len(calls) == 1, "second read should come from the cache"
    assert first == second
    database.clear_settings_cache()


@pytest.mark.asyncio
async def test_writing_a_setting_drops_the_cached_row():
    import database

    database.clear_settings_cache()
    calls = []

    async def fake_fetch(query, *args):
        calls.append(args)
        return None

    async def fake_execute(query, *args, **kwargs):
        return 1

    with patch.object(database, "_fetch_one", fake_fetch), \
         patch.object(database, "_execute", fake_execute):
        await database.get_guild_settings(42)
        await database.set_log_channel(42, 999)
        await database.get_guild_settings(42)

    assert len(calls) == 2, "a write must invalidate, or settings go stale"
    database.clear_settings_cache()


@pytest.mark.asyncio
async def test_cached_settings_cannot_be_mutated_by_a_caller():
    import database

    database.clear_settings_cache()

    async def fake_fetch(query, *args):
        return None

    with patch.object(database, "_fetch_one", fake_fetch):
        first = await database.get_guild_settings(42)
        first["log_channel_id"] = "tampered"
        second = await database.get_guild_settings(42)

    assert second["log_channel_id"] != "tampered"
    database.clear_settings_cache()
