"""Guards against self-targeting and acting on peers.

Two independent ladders decide rank: Discord role position and the bot's tier
system. They can disagree, so both are checked.
"""
import os
from unittest.mock import patch

import pytest

import guards
from guards import outranks_or_equals, refusal_reason, highest_tier_anywhere

BOT_ID = 999
MOD_ROLE, MGMT_ROLE = 10, 40


class Role:
    def __init__(self, position, id=0):
        self.position, self.id = position, id
    def __ge__(self, other):
        return self.position >= other.position


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
