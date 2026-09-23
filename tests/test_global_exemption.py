"""Tests for GLOBAL_ACTION_EXEMPT_GUILD_IDS feature.

Verifies that:
1. Global bans skip exempt guilds.
2. Global actions still propagate to non-exempt guilds.
3. Local moderation continues working inside exempt guilds.
4. Global reversals do not interfere with unrelated local moderation in exempt guilds.
5. Multiple exempt guild IDs work correctly.
6. An empty exemption list preserves existing behaviour.
"""
from unittest.mock import MagicMock, patch

import pytest


def _make_guild(guild_id: int, name: str = "Test") -> MagicMock:
    guild = MagicMock()
    guild.id = guild_id
    guild.name = name
    return guild


def _make_bot(guilds: list[MagicMock]) -> MagicMock:
    bot = MagicMock()
    bot.guilds = guilds
    return bot


class TestTargetGuildsExemption:
    """Tests for the target_guilds function with exempt guild IDs."""

    def test_exempt_guilds_excluded_from_global_actions(self):
        """Global bans skip exempt guilds."""
        from cogs.global_moderation import target_guilds

        guilds = [_make_guild(1, "Main"), _make_guild(2, "Appeals"), _make_guild(3, "Other")]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", set()), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", {2}):
            result = target_guilds(bot)
            result_ids = {g.id for g in result}
            assert 2 not in result_ids
            assert 1 in result_ids
            assert 3 in result_ids

    def test_global_actions_propagate_to_non_exempt(self):
        """Global actions still propagate to non-exempt guilds."""
        from cogs.global_moderation import target_guilds

        guilds = [_make_guild(1), _make_guild(2), _make_guild(3)]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", set()), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", {2}):
            result = target_guilds(bot)
            assert len(result) == 2
            result_ids = {g.id for g in result}
            assert result_ids == {1, 3}

    def test_multiple_exempt_guild_ids(self):
        """Multiple exempt guild IDs work correctly."""
        from cogs.global_moderation import target_guilds

        guilds = [_make_guild(i) for i in range(1, 6)]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", set()), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", {2, 4}):
            result = target_guilds(bot)
            result_ids = {g.id for g in result}
            assert result_ids == {1, 3, 5}

    def test_empty_exemption_preserves_existing_behaviour(self):
        """An empty exemption list preserves existing behaviour."""
        from cogs.global_moderation import target_guilds

        guilds = [_make_guild(1), _make_guild(2), _make_guild(3)]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", set()), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", set()):
            result = target_guilds(bot)
            assert len(result) == 3

    def test_exempt_with_approved_guilds(self):
        """Exemption works together with the approved guild list."""
        from cogs.global_moderation import target_guilds

        guilds = [_make_guild(1), _make_guild(2), _make_guild(3), _make_guild(4)]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", {1, 2, 3}), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", {2}):
            result = target_guilds(bot)
            result_ids = {g.id for g in result}
            assert result_ids == {1, 3}
            assert 2 not in result_ids
            assert 4 not in result_ids

    def test_global_reversal_skips_exempt_guilds(self):
        """Global reversals (unban) do not touch exempt guilds.

        Since target_guilds excludes exempt guilds, globalunban won't
        attempt to unban in the exempt guild, preserving any local
        moderation state there.
        """
        from cogs.global_moderation import target_guilds

        exempt_guild = _make_guild(2, "Appeals")
        guilds = [_make_guild(1, "Main"), exempt_guild, _make_guild(3, "Other")]
        bot = _make_bot(guilds)

        with patch("cogs.global_moderation.APPROVED_GUILD_IDS", set()), \
             patch("cogs.global_moderation.GLOBAL_ACTION_EXEMPT_GUILD_IDS", {2}):
            result = target_guilds(bot)
            assert exempt_guild not in result


class TestLocalModerationNotAffected:
    """Verify that exempt guilds still support normal per-guild moderation.

    The exemption is purely in target_guilds() — it doesn't disable
    any cog or command for the guild. Per-guild commands like /kick, /ban
    etc. don't go through target_guilds at all.
    """

    def test_guards_work_in_exempt_guild(self):
        """guards.refusal_reason doesn't check exemption — local moderation works."""
        from guards import refusal_reason

        actor = MagicMock()
        actor.id = 100
        actor.guild.owner_id = 999
        actor.top_role = MagicMock()
        actor.top_role.__ge__ = MagicMock(return_value=False)

        target = MagicMock()
        target.id = 200
        target.guild = actor.guild
        target.guild.owner_id = 999
        target.top_role = MagicMock()
        target.top_role.__ge__ = MagicMock(return_value=False)

        with patch("guards.PROTECTED_USER_IDS", set()), \
             patch("guards.OWNER_IDS", set()):
            result = refusal_reason(actor, target, bot_user_id=300)
            assert result is None
