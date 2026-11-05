from collections.abc import Iterable

import discord
from discord.ext import commands

from config import (
    APPROVED_GUILD_IDS,
    BAN_PERM_ROLE_IDS,
    BLOCKED_USER_IDS,
    CR_ROLE_IDS,
    DEVELOPMENT_USER_IDS,
    MANAGEMENT_ROLE_IDS,
    MOD_ROLE_IDS,
    OWNER_IDS,
    OWNERSHIP_ROLE_IDS,
    PROTECTED_USER_IDS,
)

# Tier hierarchy, lowest to highest. Each tier grants access to everything
# at its level and below.
_TIERS = ("mod", "ban_perm", "cr", "management", "ownership", "development")

_TIER_ROLE_IDS: dict[str, set[int]] = {
    "mod": MOD_ROLE_IDS,
    "ban_perm": BAN_PERM_ROLE_IDS,
    "cr": CR_ROLE_IDS,
    "management": MANAGEMENT_ROLE_IDS,
    "ownership": OWNERSHIP_ROLE_IDS,
}

# Tiers checked by user ID instead of role ID.
_TIER_USER_IDS: dict[str, set[int]] = {
    "development": DEVELOPMENT_USER_IDS,
}

_TIER_LABELS: dict[str, str] = {
    "mod": "Moderator",
    "ban_perm": "Ban Permissions",
    "cr": "Chief Ranks",
    "management": "Management Team",
    "ownership": "Ownership",
    "development": "Development",
}

_TIER_DENIALS: dict[str, str] = {
    "mod": "Only users who are part of the **North Florida Moderation Team+** can use this command.",
    "ban_perm": "Only members with **Ban Permissions+** can use this command.",
    "cr": "Only users who are **Chief Ranks+** can use this command.",
    "management": "Only users who are **Management Team+** can use this command.",
    "ownership": "Only members with **Ownership+** can use this command.",
    "development": "Can't use this shit lil boi, bot dev only.",
}


class BlockedUser(commands.CheckFailure):
    """Raised when someone on BLOCKED_USER_IDS tries to run any command."""


def has_tier(tier: str):
    """Command check requiring the user to hold a role at *tier* or above.

    OWNER_IDS always pass. The tier hierarchy from lowest to highest is:
    mod → ban_perm → cr → management → ownership → development.
    """
    tier_index = _TIERS.index(tier)
    denial = _TIER_DENIALS[tier]

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in OWNER_IDS:
            return True

        # User-ID tiers carry no guild context, so they resolve in DMs too. Checked
        # before the guild bail below, which would otherwise reject a development
        # user running a DM-capable command.
        for t in _TIERS[tier_index:]:
            if t in _TIER_USER_IDS and ctx.author.id in _TIER_USER_IDS[t]:
                return True

        if ctx.guild is None:
            return False

        author_role_ids = {role.id for role in ctx.author.roles}
        for t in _TIERS[tier_index:]:
            if t in _TIER_ROLE_IDS and author_role_ids & _TIER_ROLE_IDS[t]:
                return True

        raise commands.CheckFailure(denial)

    predicate.nfpd_tier = tier
    return commands.check(predicate)


def member_tier_index(user: discord.abc.User) -> int | None:
    """Position in _TIERS of the highest tier this user holds, or None for no tier.

    Owners count as the top tier. Roles are only visible on a Member, so a plain User
    (e.g. in DMs) can only qualify through OWNER_IDS or the user-ID tiers.
    """
    if user.id in OWNER_IDS:
        return len(_TIERS) - 1
    role_ids = {role.id for role in getattr(user, "roles", ())}
    for index in range(len(_TIERS) - 1, -1, -1):
        tier = _TIERS[index]
        if tier in _TIER_USER_IDS and user.id in _TIER_USER_IDS[tier]:
            return index
        if tier in _TIER_ROLE_IDS and role_ids & _TIER_ROLE_IDS[tier]:
            return index
    return None


def tier_for_role_ids(role_ids: Iterable[int]) -> int | None:
    """Highest tier granted by these role IDs, or None if they grant no tier.

    Lets a rank be judged from roles the member no longer holds - a suspended
    member has had theirs stripped, so their live roles say nothing about the
    rank they are being restored to.
    """
    held = set(role_ids)
    for index in range(len(_TIERS) - 1, -1, -1):
        if held & _TIER_ROLE_IDS.get(_TIERS[index], set()):
            return index
    return None


def tier_index(tier: str) -> int:
    return _TIERS.index(tier)


def tier_granting_role_ids(user: discord.abc.User) -> list[int]:
    """Role IDs this user holds that grant a staff tier, highest tier first."""
    role_ids = {role.id for role in getattr(user, "roles", ())}
    granting: list[int] = []
    for index in range(len(_TIERS) - 1, -1, -1):
        granting.extend(sorted(role_ids & _TIER_ROLE_IDS.get(_TIERS[index], set())))
    return granting


def special_grants(user: discord.abc.User) -> list[str]:
    """Labels for access this user gets by user ID rather than by role."""
    grants: list[str] = []
    if user.id in OWNER_IDS:
        grants.append("Bot owner — passes every tier check")
    for tier, ids in _TIER_USER_IDS.items():
        if user.id in ids:
            grants.append(f"{_TIER_LABELS[tier]} (granted by user ID)")
    return grants


def tier_label(index: int | None) -> str:
    return "No staff tier" if index is None else _TIER_LABELS[_TIERS[index]]


def highest_tier_anywhere(bot, user: discord.abc.User) -> int | None:
    """The highest tier this user holds in any guild the bot can see.

    Global actions cross server boundaries, where Discord role position means
    nothing, so tier is the only comparison that holds. Taking the highest tier
    found anywhere is the cautious reading: a user who is Management in one NFPD
    server is treated as Management for global actions everywhere.
    """
    best = member_tier_index(user)  # catches OWNER_IDS and the user-ID tiers
    for guild in bot.guilds:
        member = guild.get_member(user.id)
        if member is None:
            continue
        tier = member_tier_index(member)
        if tier is not None and (best is None or tier > best):
            best = tier
    return best


def from_approved_guild():
    """Command check ensuring the command is run from an approved server.

    Passes when APPROVED_GUILD_IDS is empty (no allowlist configured).
    """

    async def predicate(ctx: commands.Context) -> bool:
        if not APPROVED_GUILD_IDS:
            return True
        if ctx.guild is not None and ctx.guild.id in APPROVED_GUILD_IDS:
            return True
        raise commands.CheckFailure("Global commands can only be used from an approved server.")

    return commands.check(predicate)


def is_bot_owner():
    """Command check restricting a command to OWNER_IDS.

    Kept for backward compatibility. New commands should use
    has_tier("development") instead.
    """

    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id in OWNER_IDS

    return commands.check(predicate)


def is_blocked(user_id: int) -> bool:
    """Users on the deny list cannot use the bot at all."""
    return user_id in BLOCKED_USER_IDS


# Protected users added through /protect, mirrored in memory because is_protected
# is synchronous and sits on the hot path of every moderation command - it cannot
# await a query. Loaded once at startup and rewritten by the commands that change
# it, so the database stays the record and this stays the lookup.
_runtime_protected: set[int] = set()


def set_runtime_protected(user_ids: set[int]) -> None:
    """Replace the in-memory mirror of the database-backed protected list."""
    global _runtime_protected
    _runtime_protected = set(user_ids)


def runtime_protected_ids() -> set[int]:
    return set(_runtime_protected)


def is_protected(user_id: int) -> bool:
    """Users who can never be the target of a moderation action.

    Owners are always included on top of PROTECTED_USER_IDS, so a moderator whose account
    is compromised still can't ban the people who administer the bot.

    The runtime list adds to those two rather than replacing them: a user protected
    by the environment cannot be unprotected by a command.
    """
    return (
        user_id in PROTECTED_USER_IDS
        or user_id in OWNER_IDS
        or user_id in _runtime_protected
    )


def outranks_or_equals(actor: discord.Member, target: discord.Member) -> bool:
    """True when the target sits at or above the actor, by EITHER measure.

    Discord role position and the bot's tier ladder are independent: a Moderator
    whose role happens to sit high in the role list would otherwise be able to
    action a Management member whose role sits low. Either measure placing the
    target level with or above the actor is enough to refuse.
    """
    if actor.id == actor.guild.owner_id:
        return False

    if target.top_role >= actor.top_role:
        return True

    target_tier = member_tier_index(target)
    if target_tier is None:
        return False  # No staff tier, so the role position above is the only say.
    actor_tier = member_tier_index(actor)
    if actor_tier is None:
        return True
    return target_tier >= actor_tier


def refusal_reason(
    actor: discord.Member,
    target: discord.abc.User,
    bot_user_id: int,
    *,
    check_hierarchy: bool = True,
) -> str | None:
    """Why a moderation action against this target must not proceed, or None if it may.

    Consolidates the self/bot/protected/owner/hierarchy checks that every moderation
    command needs, so they can't drift apart or be forgotten on a new command.
    """
    if target.id == actor.id:
        return "You can't use that on yourself."
    if target.id == bot_user_id:
        return "You can't use that on me."
    if is_protected(target.id):
        return f"**{target}** is on the protected list and can't be moderated."

    if isinstance(target, discord.Member):
        if target.id == target.guild.owner_id:
            return f"**{target}** owns this server and can't be moderated."
        if check_hierarchy and outranks_or_equals(actor, target):
            return "You can't action someone ranked equal to or above you."

    return None
