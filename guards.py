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
        if ctx.guild is None:
            return False

        author_role_ids = {role.id for role in ctx.author.roles}
        for t in _TIERS[tier_index:]:
            if t in _TIER_USER_IDS and ctx.author.id in _TIER_USER_IDS[t]:
                return True
            if t in _TIER_ROLE_IDS and author_role_ids & _TIER_ROLE_IDS[t]:
                return True

        raise commands.CheckFailure(denial)

    return commands.check(predicate)


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


def is_protected(user_id: int) -> bool:
    """Users who can never be the target of a moderation action.

    Owners are always included on top of PROTECTED_USER_IDS, so a moderator whose account
    is compromised still can't ban the people who administer the bot.
    """
    return user_id in PROTECTED_USER_IDS or user_id in OWNER_IDS


def outranks_or_equals(actor: discord.Member, target: discord.Member) -> bool:
    """True when the target sits at or above the actor in the role hierarchy."""
    if actor.id == actor.guild.owner_id:
        return False
    return target.top_role >= actor.top_role


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
