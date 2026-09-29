import logging

import discord
from discord.ext import commands

from database import add_case, get_guild_settings
from embeds import build_case_embed

logger = logging.getLogger("modbot.modlog")


async def _resolve_channel(guild: discord.Guild, channel_id: int) -> discord.abc.Messageable | None:
    """Return the channel, using the cache first and falling back to an API fetch.

    guild.get_channel() only checks the bot's in-memory cache. After a restart or
    reconnect that cache can be empty, causing every log post to silently vanish.
    fetch_channel() goes directly to the Discord API and always returns the real channel.
    """
    channel = guild.get_channel(channel_id)
    if channel is not None and isinstance(channel, discord.abc.Messageable):
        return channel

    try:
        channel = await guild.fetch_channel(channel_id)
    except discord.NotFound:
        logger.warning(
            "Log channel %s in guild %s (%s) no longer exists - use /setlogchannel to set a new one",
            channel_id, guild.name, guild.id,
        )
        return None
    except discord.Forbidden:
        logger.warning(
            "Log channel %s in guild %s (%s) exists but I cannot access it (missing View Channel)",
            channel_id, guild.name, guild.id,
        )
        return None
    except discord.HTTPException as error:
        logger.warning(
            "Could not fetch log channel %s in guild %s (%s): %s",
            channel_id, guild.name, guild.id, error,
        )
        return None

    if not isinstance(channel, discord.abc.Messageable):
        logger.warning(
            "Log channel %s in guild %s (%s) is not a messageable type (%s)",
            channel_id, guild.name, guild.id, type(channel).__name__,
        )
        return None

    return channel


async def _send_to_channel(guild: discord.Guild, channel_id: int, embed: discord.Embed) -> None:
    channel = await _resolve_channel(guild, channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        logger.warning(
            "Missing Send Messages or Embed Links in log channel %s in guild %s (%s)",
            channel_id, guild.name, guild.id,
        )
    except discord.HTTPException as error:
        logger.warning(
            "Could not post to log channel in guild %s (%s): %s",
            guild.name, guild.id, error,
        )


async def post_to_log_channel(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post a moderation case embed to the guild's mod-log channel."""
    settings = await get_guild_settings(guild.id)
    channel_id = settings["log_channel_id"]
    if channel_id is None:
        return
    await _send_to_channel(guild, channel_id, embed)


async def post_to_alt_log_channel(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post an alt-detection embed to the guild's alt-log channel.

    Falls back to the server-log channel, then the mod-log channel.
    """
    settings = await get_guild_settings(guild.id)
    channel_id = (
        settings.get("alt_log_channel_id")
        or settings.get("server_log_channel_id")
        or settings.get("log_channel_id")
    )
    if channel_id is None:
        return
    await _send_to_channel(guild, channel_id, embed)


async def post_to_server_log_channel(guild: discord.Guild, embed: discord.Embed) -> None:
    """Post a server event embed to the server-log channel.

    Falls back to the main mod-log channel if no separate server-log channel is set,
    so existing setups that use one channel for everything continue to work.
    """
    settings = await get_guild_settings(guild.id)
    channel_id = settings.get("server_log_channel_id") or settings.get("log_channel_id")
    if channel_id is None:
        return
    await _send_to_channel(guild, channel_id, embed)


async def check_log_channel(guild: discord.Guild) -> tuple[bool, str]:
    """Diagnose why the configured mod-log channel would or wouldn't receive a message."""
    settings = await get_guild_settings(guild.id)
    channel_id = settings["log_channel_id"]
    if channel_id is None:
        return False, "No mod-log channel configured. Set one with /setlogchannel."

    return await _check_channel_id(guild, channel_id)


async def check_server_log_channel(guild: discord.Guild) -> tuple[bool, str]:
    """Diagnose the server-log channel (or fall back to mod-log channel)."""
    settings = await get_guild_settings(guild.id)
    channel_id = settings.get("server_log_channel_id")
    if channel_id is None:
        fallback_id = settings.get("log_channel_id")
        if fallback_id is None:
            return False, "No server-log or mod-log channel configured."
        return await _check_channel_id(guild, fallback_id, label="mod-log (fallback)")
    return await _check_channel_id(guild, channel_id)


async def _check_channel_id(
    guild: discord.Guild, channel_id: int, *, label: str = ""
) -> tuple[bool, str]:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return False, f"Configured channel `{channel_id}` no longer exists."
        except discord.Forbidden:
            return False, f"Channel `{channel_id}` exists but I cannot access it - missing View Channel."
        except discord.HTTPException as error:
            return False, f"Could not fetch channel `{channel_id}`: {error}"

    if not isinstance(channel, discord.abc.Messageable):
        return False, f"{channel.mention} is not a type the bot can send messages to."

    if guild.me is not None:
        permissions = channel.permissions_for(guild.me)
        missing = [
            name for name, has_it in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
            )
            if not has_it
        ]
        if missing:
            return False, f"{channel.mention} is set, but I am missing: {', '.join(missing)}."

    suffix = f" ({label})" if label else ""
    return True, f"{channel.mention} is set and reachable{suffix}."


async def record_case(
    guild: discord.Guild,
    target: discord.abc.User,
    moderator: discord.abc.User,
    action_type: str,
    reason: str,
) -> discord.Embed:
    """Save the case, post it to the mod-log channel, and return the embed."""
    case_id = await add_case(guild.id, target.id, moderator.id, action_type, reason)
    embed = build_case_embed(action_type, target, moderator, reason, case_id)
    await post_to_log_channel(guild, embed)
    return embed


async def announce_case(
    ctx: commands.Context,
    target: discord.abc.User,
    action_type: str,
    reason: str,
    moderator: discord.abc.User | None = None,
) -> discord.Embed:
    """record_case, plus the reply in the channel the command was run from."""
    embed = await record_case(ctx.guild, target, moderator or ctx.author, action_type, reason)
    await ctx.send(embed=embed)
    return embed


async def try_dm(user: discord.abc.User, embed: discord.Embed, view: discord.ui.View | None = None) -> bool:
    """DMs fail for closed DMs (403), bots and blocked senders (400), and deleted accounts (404)."""
    try:
        await user.send(embed=embed, view=view)
        return True
    except discord.HTTPException:
        return False
