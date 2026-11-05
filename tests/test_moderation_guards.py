"""Guards against self-targeting and acting on peers.

Two independent ladders decide rank: Discord role position and the bot's tier
system. They can disagree, so both are checked.
"""
import os
from unittest.mock import patch

import pytest

import guards
import cogs.moderation as mod   # imported before the fixture patches discord.Member
from guards import outranks_or_equals, refusal_reason, highest_tier_anywhere

BOT_ID = 999
MOD_ROLE, MGMT_ROLE = 10, 40


class Role:
    def __init__(self, position, id=0):
        self.position, self.id = position, id
    def __ge__(self, other):
        return self.position >= other.position
    def __gt__(self, other):
        return self.position > other.position


class Guild:
    def __init__(self, owner_id=0, members=None):
        self.owner_id, self.id = owner_id, 500
        self._members = members or {}
    def get_member(self, uid):
        return self._members.get(uid)


class Member:
    """Stands in for discord.Member; patched in so isinstance() accepts it."""
    def __init__(self, id, position, role_ids=(), guild=None):
        self.id = id
        self.top_role = Role(position)
        self.roles = [Role(0, r) for r in role_ids]
        self.guild = guild or Guild()
    def __str__(self):
        return f"User{self.id}"


class Bot:
    def __init__(self, guilds):
        self.guilds = guilds


@pytest.fixture(autouse=True)
def tier_config():
    with patch.object(guards, "MOD_ROLE_IDS", {MOD_ROLE}), \
         patch.object(guards, "MANAGEMENT_ROLE_IDS", {MGMT_ROLE}), \
         patch.object(guards, "_TIER_ROLE_IDS",
                      {"mod": {MOD_ROLE}, "ban_perm": set(), "cr": set(),
                       "management": {MGMT_ROLE}, "ownership": set()}), \
         patch.object(guards, "OWNER_IDS", set()), \
         patch.object(guards, "PROTECTED_USER_IDS", set()), \
         patch.object(guards.discord, "Member", Member):
        yield


# --- per-guild commands ------------------------------------------------------

def test_cannot_action_self():
    actor = Member(1, 5, [MOD_ROLE])
    assert refusal_reason(actor, actor, BOT_ID) == "You can't use that on yourself."


def test_cannot_action_the_bot():
    actor = Member(1, 5, [MOD_ROLE])
    assert refusal_reason(actor, Member(BOT_ID, 1), BOT_ID) is not None


def test_cannot_action_equal_role_position():
    actor, peer = Member(1, 5, [MOD_ROLE]), Member(2, 5, [MOD_ROLE])
    assert outranks_or_equals(actor, peer)


def test_can_action_someone_plainly_below():
    actor, target = Member(1, 5, [MOD_ROLE]), Member(2, 1, [])
    assert not outranks_or_equals(actor, target)


def test_tier_outranks_even_when_role_position_is_lower():
    """The gap this closes: a Moderator whose role sits high in the list must
    still not be able to action a Management member whose role sits low."""
    mod_high_role = Member(1, 99, [MOD_ROLE])
    mgmt_low_role = Member(2, 1, [MGMT_ROLE])
    assert outranks_or_equals(mod_high_role, mgmt_low_role)


def test_equal_tier_is_refused_regardless_of_position():
    a = Member(1, 99, [MGMT_ROLE])
    b = Member(2, 1, [MGMT_ROLE])
    assert outranks_or_equals(a, b)


def test_guild_owner_is_never_outranked():
    owner_guild = Guild(owner_id=1)
    actor = Member(1, 1, [], guild=owner_guild)
    assert not outranks_or_equals(actor, Member(2, 99, [MGMT_ROLE]))


# --- global commands ---------------------------------------------------------

def test_highest_tier_anywhere_finds_tier_in_another_guild():
    mgmt = Member(7, 1, [MGMT_ROLE])
    bot = Bot([Guild(members={}), Guild(members={7: mgmt})])
    assert highest_tier_anywhere(bot, mgmt) == guards.tier_index("management")


def test_highest_tier_anywhere_is_none_for_a_regular_user():
    plain = Member(8, 1, [])
    assert highest_tier_anywhere(Bot([Guild(members={8: plain})]), plain) is None


# --- warn auto-escalation ----------------------------------------------------

@pytest.mark.asyncio
async def test_warn_escalation_cannot_punish_someone_you_cannot_ban():
    """/warn skips the hierarchy check so peers can be warned. The automatic
    punishment must not become a way around that: warning a superior repeatedly
    must not ban them."""
    from unittest.mock import AsyncMock, MagicMock

    target = MagicMock()
    target.mention = "@boss"
    target.ban = AsyncMock()
    target.kick = AsyncMock()
    target.timeout = AsyncMock()

    ctx = MagicMock()
    ctx.guild.id = 1
    ctx.send = AsyncMock()

    bot = MagicMock()
    bot.user.id = BOT_ID
    cog = mod.Moderation(bot)

    settings = {"warn_ban_threshold": 3, "warn_kick_threshold": None,
                "warn_mute_threshold": None, "warn_mute_minutes": None}

    seen = {}

    def spy(actor, tgt, bot_id, **kwargs):
        seen.update(actor=actor, target=tgt, bot_id=bot_id, kwargs=kwargs)
        return "You can't action someone ranked equal to or above you."

    with patch.object(mod, "get_guild_settings", AsyncMock(return_value=settings)), \
         patch.object(mod, "refusal_reason", spy), \
         patch.object(mod, "announce_case", AsyncMock()) as announced:
        await cog.escalate_if_needed(ctx, target, 3)

    # The check must run against the real actor/target, with the hierarchy
    # comparison ENABLED - that is the bypass being closed. Without these the
    # test would still pass if the call inverted the pair or passed
    # check_hierarchy=False, which is exactly the bug.
    assert seen["actor"] is ctx.author
    assert seen["target"] is target
    assert seen["bot_id"] == BOT_ID
    assert seen["kwargs"].get("check_hierarchy", True) is True

    target.ban.assert_not_called()
    target.kick.assert_not_called()
    target.timeout.assert_not_called()
    announced.assert_not_called()
    ctx.send.assert_awaited()          # the moderator is told why


@pytest.mark.asyncio
async def test_warn_escalation_still_fires_on_a_valid_target():
    from unittest.mock import AsyncMock, MagicMock

    target = MagicMock()
    target.ban = AsyncMock()
    ctx = MagicMock()
    ctx.guild.id = 1
    ctx.send = AsyncMock()
    bot = MagicMock()
    bot.user.id = BOT_ID
    cog = mod.Moderation(bot)

    settings = {"warn_ban_threshold": 3, "warn_kick_threshold": None,
                "warn_mute_threshold": None, "warn_mute_minutes": None}

    with patch.object(mod, "get_guild_settings", AsyncMock(return_value=settings)), \
         patch.object(mod, "refusal_reason", lambda *a, **k: None), \
         patch.object(mod, "announce_case", AsyncMock()):
        await cog.escalate_if_needed(ctx, target, 3)

    target.ban.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_skipped_escalation_is_retried_on_a_later_warn():
    """With ==, an escalation withheld at exactly the threshold consumed it
    permanently: warn counts past it never matched again. A later warn, once the
    target can legitimately be actioned, must still apply the punishment."""
    from unittest.mock import AsyncMock, MagicMock

    target = MagicMock()
    target.ban = AsyncMock()
    ctx = MagicMock()
    ctx.guild.id = 1
    ctx.send = AsyncMock()
    bot = MagicMock()
    bot.user.id = BOT_ID
    cog = mod.Moderation(bot)

    settings = {"warn_ban_threshold": 3, "warn_kick_threshold": None,
                "warn_mute_threshold": None, "warn_mute_minutes": None}

    # Warn 5, well past the threshold of 3, because 3 was withheld earlier.
    with patch.object(mod, "get_guild_settings", AsyncMock(return_value=settings)), \
         patch.object(mod, "refusal_reason", lambda *a, **k: None), \
         patch.object(mod, "announce_case", AsyncMock()):
        await cog.escalate_if_needed(ctx, target, 5)

    target.ban.assert_awaited_once()


@pytest.mark.asyncio
async def test_thresholds_pick_the_most_severe_one_crossed():
    """setwarnthresholds enforces mute < kick < ban, so a count past several
    thresholds must take the most severe, not the first configured."""
    from unittest.mock import AsyncMock, MagicMock

    settings = {"warn_ban_threshold": 3, "warn_kick_threshold": 2,
                "warn_mute_threshold": 1, "warn_mute_minutes": 60}
    expected = {1: "mute", 2: "kick", 3: "ban", 9: "ban"}

    for warn_count, want in expected.items():
        target = MagicMock()
        target.ban, target.kick, target.timeout = AsyncMock(), AsyncMock(), AsyncMock()
        ctx = MagicMock(); ctx.guild.id = 1; ctx.send = AsyncMock()
        bot = MagicMock(); bot.user.id = BOT_ID
        cog = mod.Moderation(bot)

        with patch.object(mod, "get_guild_settings", AsyncMock(return_value=settings)), \
             patch.object(mod, "refusal_reason", lambda *a, **k: None), \
             patch.object(mod, "announce_case", AsyncMock()):
            await cog.escalate_if_needed(ctx, target, warn_count)

        called = {"ban": target.ban, "kick": target.kick, "mute": target.timeout}
        assert called[want].await_count == 1, f"warn {warn_count} should {want}"
        for name, m in called.items():
            if name != want:
                assert m.await_count == 0, f"warn {warn_count} should not {name}"


@pytest.mark.asyncio
async def test_disabled_thresholds_never_escalate():
    from unittest.mock import AsyncMock, MagicMock

    target = MagicMock()
    target.ban, target.kick, target.timeout = AsyncMock(), AsyncMock(), AsyncMock()
    ctx = MagicMock(); ctx.guild.id = 1; ctx.send = AsyncMock()
    bot = MagicMock(); bot.user.id = BOT_ID
    cog = mod.Moderation(bot)

    settings = {"warn_ban_threshold": None, "warn_kick_threshold": 0,
                "warn_mute_threshold": None, "warn_mute_minutes": None}

    with patch.object(mod, "get_guild_settings", AsyncMock(return_value=settings)), \
         patch.object(mod, "refusal_reason", lambda *a, **k: None), \
         patch.object(mod, "announce_case", AsyncMock()):
        await cog.escalate_if_needed(ctx, target, 50)

    target.ban.assert_not_called()
    target.kick.assert_not_called()
    target.timeout.assert_not_called()


# --- suspend is punitive, unsuspend is restorative ---------------------------

def test_suspend_refuses_someone_above_you():
    mod = Member(1, 5, [MOD_ROLE])
    boss = Member(2, 9, [MGMT_ROLE])
    assert refusal_reason(mod, boss, BOT_ID) is not None


def test_suspend_refuses_an_equal():
    """Both suspend and unsuspend use the strict check: equal or above is refused."""
    a, b = Member(1, 5, [MOD_ROLE]), Member(2, 5, [MOD_ROLE])
    assert refusal_reason(a, b, BOT_ID) is not None


def test_suspend_refuses_someone_above():
    mod, boss = Member(1, 5, [MOD_ROLE]), Member(2, 9, [MGMT_ROLE])
    assert refusal_reason(mod, boss, BOT_ID) is not None


def test_unsuspend_uses_the_same_strict_check_as_suspend():
    """unsuspend shares refusal_reason, so a peer or a superior is refused for
    both. A suspended member whose roles were stripped no longer outranks, which
    is what still makes an ordinary suspension liftable."""
    mod = Member(1, 5, [MOD_ROLE])
    peer = Member(2, 5, [MOD_ROLE])
    boss = Member(3, 9, [MGMT_ROLE])
    stripped = Member(4, 0, [])          # suspended: roles gone, so ranks below
    assert refusal_reason(mod, peer, BOT_ID) is not None
    assert refusal_reason(mod, boss, BOT_ID) is not None
    assert refusal_reason(mod, stripped, BOT_ID) is None


def test_no_command_opts_out_of_the_hierarchy_check_except_warn():
    """check_hierarchy=False is warn's alone; nothing else may relax it."""
    import inspect
    import cogs.moderation as m
    src = inspect.getsource(m)
    assert src.count("check_hierarchy=False") == 1


# --- /suspend is gated at Chief Rank ------------------------------------------

@pytest.mark.asyncio
async def test_suspend_requires_chief_rank_or_above():
    """Pins the gate: Moderator and Ban Perm cannot reach /suspend at all."""
    from discord.ext import commands as dcommands

    async def _dummy(): ...
    predicate = guards.has_tier("cr")(_dummy).__commands_checks__[0]

    BAN_PERM_ROLE, CR_ROLE, OWNERSHIP_ROLE = 20, 30, 50
    ladder = {"mod": {MOD_ROLE}, "ban_perm": {BAN_PERM_ROLE}, "cr": {CR_ROLE},
              "management": {MGMT_ROLE}, "ownership": {OWNERSHIP_ROLE}}

    class Ctx:
        def __init__(self, member):
            self.author, self.guild = member, member.guild

    expected = {
        MOD_ROLE: False, BAN_PERM_ROLE: False,
        CR_ROLE: True, MGMT_ROLE: True, OWNERSHIP_ROLE: True,
    }
    with patch.object(guards, "_TIER_ROLE_IDS", ladder), \
         patch.object(guards, "OWNER_IDS", set()):
        for role_id, may_run in expected.items():
            ctx = Ctx(Member(1, 5, [role_id]))
            try:
                got = await predicate(ctx)
            except dcommands.CheckFailure:
                got = False
            assert got is may_run, f"role {role_id} should {'pass' if may_run else 'fail'}"


def test_chief_rank_can_only_suspend_below():
    """A Chief Rank reaches Moderator and below, never a peer or above."""
    BAN_PERM_ROLE, CR_ROLE = 20, 30
    ladder = {"mod": {MOD_ROLE}, "ban_perm": {BAN_PERM_ROLE}, "cr": {CR_ROLE},
              "management": {MGMT_ROLE}, "ownership": set()}
    with patch.object(guards, "_TIER_ROLE_IDS", ladder), \
         patch.object(guards, "OWNER_IDS", set()):
        cr = Member(1, 7, [CR_ROLE])
        assert refusal_reason(cr, Member(2, 5, [MOD_ROLE]), BOT_ID) is None       # below
        assert refusal_reason(cr, Member(3, 6, [BAN_PERM_ROLE]), BOT_ID) is None  # below
        assert refusal_reason(cr, Member(4, 1, []), BOT_ID) is None               # no tier
        assert refusal_reason(cr, Member(5, 7, [CR_ROLE]), BOT_ID) is not None    # peer
        assert refusal_reason(cr, Member(6, 8, [MGMT_ROLE]), BOT_ID) is not None  # above
