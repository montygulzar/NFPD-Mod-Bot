"""Comprehensive server event logging — Quark-style audit output.

Covers: messages (delete/edit/bulk), members (join/leave/kick/ban/unban/timeout),
voice (join/leave/disconnect/move/server-mute/deafen), channels, roles, and invites.

Differentiates moderator actions from user-initiated ones via time-bounded audit
log lookups: kicks vs voluntary leaves, disconnects vs leaving voice, mod-moves
vs self-switches, and server mute/deafen changes.

Uses post_to_server_log_channel which routes to the guild's dedicated server-log
channel if configured, falling back to the mod-log channel.
"""
from __future__ import annotations

from datetime import timedelta

import discord
from discord.ext import commands

import embeds as embeds_module
from config import BRAND_NAME
from modlog import post_to_server_log_channel

# --- Colour palette -----------------------------------------------------------

COLOR_JOIN      = 0x57F287
COLOR_LEAVE     = 0x747F8D
COLOR_KICK      = 0xE67E22
COLOR_BAN       = 0xED4245
COLOR_UNBAN     = 0x57F287
COLOR_TIMEOUT   = 0xFEE75C
COLOR_DELETE    = 0xED4245
COLOR_EDIT      = 0xF5A524
COLOR_VOICE     = 0x5865F2
COLOR_VOICE_MOD = 0xEB459E
COLOR_ROLE      = 0xEB459E
COLOR_CHANNEL   = 0x57F287
COLOR_INVITE    = 0x9B59B6
COLOR_NICKNAME  = 0xFEE75C

AUDIT_WINDOW = timedelta(seconds=5)


# --- Embed builders -----------------------------------------------------------

def _embed(*, icon: str, label: str, color: int, desc: str | None = None) -> discord.Embed:
    embed = discord.Embed(description=desc, color=color, timestamp=discord.utils.utcnow())
    embed.set_author(name=f"{icon}  {label}", icon_url=embeds_module.BRAND_ICON_URL)
    embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
    return embed


def _user_embed(
    *, icon: str, label: str, color: int,
    user: discord.abc.User | discord.Member,
) -> discord.Embed:
    embed = discord.Embed(
        description=f"**{user}**\n`{user.id}`",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=f"{icon}  {label}", icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
    return embed


def _short(text: str | None, limit: int = 1024) -> str:
    if not text or not text.strip():
        return "*None*"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


# --- Audit log helpers --------------------------------------------------------

async def _get_audit_entry(
    guild: discord.Guild,
    target_id: int,
    action: discord.AuditLogAction,
) -> tuple[str | None, discord.abc.User | None]:
    if not guild.me or not guild.me.guild_permissions.view_audit_log:
        return None, None
    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            if entry.target and entry.target.id == target_id:
                return entry.reason, entry.user
    except discord.HTTPException:
        pass
    return None, None


async def _get_recent_audit_entry(
    guild: discord.Guild,
    target_id: int,
    action: discord.AuditLogAction,
) -> tuple[str | None, discord.abc.User | None]:
    """Match by target AND recency — prevents stale entries from producing
    false positives when a member leaves voluntarily hours after someone
    else was kicked."""
    if not guild.me or not guild.me.guild_permissions.view_audit_log:
        return None, None
    now = discord.utils.utcnow()
    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            if entry.target and entry.target.id == target_id:
                if (now - entry.created_at) <= AUDIT_WINDOW:
                    return entry.reason, entry.user
                return None, None
    except discord.HTTPException:
        pass
    return None, None


async def _check_voice_audit(
    guild: discord.Guild,
    action: discord.AuditLogAction,
) -> discord.abc.User | None:
    """Check for a recent voice mod action (disconnect/move).  These audit
    entries can lack a target_id in the Discord API, so matching relies on
    the time window alone."""
    if not guild.me or not guild.me.guild_permissions.view_audit_log:
        return None
    now = discord.utils.utcnow()
    try:
        async for entry in guild.audit_logs(limit=3, action=action):
            if (now - entry.created_at) <= AUDIT_WINDOW:
                return entry.user
            break
    except discord.HTTPException:
        pass
    return None


# --- Cog ----------------------------------------------------------------------

class ServerLogs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- Messages -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        _, moderator = await _get_audit_entry(
            message.guild, message.author.id, discord.AuditLogAction.message_delete,
        )
        mod_action = moderator is not None and moderator.id != message.author.id

        embed = _embed(
            icon="\U0001F5D1",
            label="Message Purged" if mod_action else "Message Deleted",
            color=COLOR_DELETE,
        )
        embed.add_field(name="Author", value=f"{message.author.mention} `{message.author.id}`", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        if mod_action:
            embed.add_field(name="Deleted by", value=moderator.mention, inline=True)
        if message.content:
            embed.add_field(name="Content", value=_short(message.content, 900), inline=False)
        if message.attachments:
            embed.add_field(
                name=f"Attachments ({len(message.attachments)})",
                value=_short("\n".join(f"`{a.filename}`" for a in message.attachments)),
                inline=False,
            )
        await post_to_server_log_channel(message.guild, embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        non_bot = [m for m in messages if not m.author.bot]
        _, moderator = await _get_audit_entry(
            guild, messages[0].channel.id, discord.AuditLogAction.message_bulk_delete,
        )
        embed = _embed(icon="\U0001F5D1", label="Bulk Message Delete", color=COLOR_DELETE)
        embed.add_field(name="Channel", value=messages[0].channel.mention, inline=True)
        embed.add_field(name="User messages", value=str(len(non_bot)), inline=True)
        embed.add_field(name="Total removed", value=str(len(messages)), inline=True)
        if moderator:
            embed.add_field(name="Purged by", value=moderator.mention, inline=True)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return

        embed = _embed(icon="✏️", label="Message Edited", color=COLOR_EDIT)
        embed.add_field(name="Author", value=f"{before.author.mention} `{before.author.id}`", inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump", value=f"[View message]({after.jump_url})", inline=True)
        embed.add_field(name="Before", value=_short(before.content, 512), inline=False)
        embed.add_field(name="After", value=_short(after.content, 512), inline=False)
        await post_to_server_log_channel(before.guild, embed)

    # --- Members --------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        age = discord.utils.utcnow() - member.created_at
        age_days = age.days

        embed = _user_embed(icon="\U0001F44B", label="Member Joined", color=COLOR_JOIN, user=member)
        embed.add_field(
            name="Account age",
            value=f"{discord.utils.format_dt(member.created_at, 'R')}\n({age_days}d old)",
            inline=True,
        )
        embed.add_field(name="Member #", value=str(member.guild.member_count), inline=True)
        if age_days < 7:
            embed.add_field(
                name="⚠  New account",
                value=f"Only **{age_days} day(s)** old",
                inline=False,
            )
        await post_to_server_log_channel(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild

        reason, moderator = await _get_recent_audit_entry(
            guild, member.id, discord.AuditLogAction.kick,
        )

        if moderator:
            embed = _user_embed(icon="\U0001F6AA", label="Member Kicked", color=COLOR_KICK, user=member)
            embed.add_field(name="Kicked by", value=moderator.mention, inline=True)
            if reason:
                embed.add_field(name="Reason", value=_short(reason), inline=False)
        else:
            embed = _user_embed(icon="\U0001F4A8", label="Member Left", color=COLOR_LEAVE, user=member)

        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        roles = [r.mention for r in member.roles if r != guild.default_role]
        if roles:
            embed.add_field(name="Roles", value=_short(", ".join(roles)), inline=False)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        reason, moderator = await _get_audit_entry(guild, user.id, discord.AuditLogAction.ban)
        embed = _user_embed(icon="\U0001F6D1", label="Member Banned", color=COLOR_BAN, user=user)
        if moderator:
            embed.add_field(name="Banned by", value=moderator.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=_short(reason), inline=False)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        reason, moderator = await _get_audit_entry(guild, user.id, discord.AuditLogAction.unban)
        embed = _user_embed(icon="\U0001F513", label="Member Unbanned", color=COLOR_UNBAN, user=user)
        if moderator:
            embed.add_field(name="Unbanned by", value=moderator.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=_short(reason), inline=False)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = before.guild

        # --- Timeout changes --------------------------------------------------
        now = discord.utils.utcnow()
        was_timed_out = before.timed_out_until is not None and before.timed_out_until > now
        is_timed_out = after.timed_out_until is not None and after.timed_out_until > now

        if not was_timed_out and is_timed_out:
            _, moderator = await _get_audit_entry(guild, after.id, discord.AuditLogAction.member_update)
            embed = _user_embed(icon="⏰", label="Member Timed Out", color=COLOR_TIMEOUT, user=after)
            if moderator:
                embed.add_field(name="Timed out by", value=moderator.mention, inline=True)
            embed.add_field(
                name="Expires",
                value=discord.utils.format_dt(after.timed_out_until, "R"),
                inline=True,
            )
            await post_to_server_log_channel(guild, embed)

        elif was_timed_out and not is_timed_out:
            _, moderator = await _get_audit_entry(guild, after.id, discord.AuditLogAction.member_update)
            embed = _user_embed(icon="⏰", label="Timeout Removed", color=COLOR_UNBAN, user=after)
            if moderator and moderator.id != after.id:
                embed.add_field(name="Removed by", value=moderator.mention, inline=True)
            await post_to_server_log_channel(guild, embed)

        # --- Nickname changes -------------------------------------------------
        if before.nick != after.nick:
            _, moderator = await _get_audit_entry(guild, after.id, discord.AuditLogAction.member_update)
            embed = _user_embed(
                icon="\U0001F3F7️", label="Nickname Changed", color=COLOR_NICKNAME, user=after,
            )
            embed.add_field(name="Before", value=before.nick or "*None*", inline=True)
            embed.add_field(name="After", value=after.nick or "*None*", inline=True)
            if moderator and moderator.id != after.id:
                embed.add_field(name="Changed by", value=moderator.mention, inline=True)
            await post_to_server_log_channel(guild, embed)

        # --- Role changes -----------------------------------------------------
        added = [r for r in after.roles if r not in before.roles and r != guild.default_role]
        removed = [r for r in before.roles if r not in after.roles and r != guild.default_role]
        if added or removed:
            _, moderator = await _get_audit_entry(guild, after.id, discord.AuditLogAction.member_role_update)
            embed = _user_embed(
                icon="\U0001F6E1️", label="Roles Updated", color=COLOR_ROLE, user=after,
            )
            if moderator:
                embed.add_field(name="Updated by", value=moderator.mention, inline=True)
            if added:
                embed.add_field(name="Added", value=_short(", ".join(r.mention for r in added)), inline=False)
            if removed:
                embed.add_field(name="Removed", value=_short(", ".join(r.mention for r in removed)), inline=False)
            await post_to_server_log_channel(guild, embed)

    # --- Voice ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild

        # --- Channel changes --------------------------------------------------
        if before.channel != after.channel:
            if before.channel is None and after.channel is not None:
                embed = _embed(
                    icon="\U0001F50A", label="Joined Voice", color=COLOR_VOICE,
                    desc=f"{member.mention} joined {after.channel.mention}",
                )
                await post_to_server_log_channel(guild, embed)

            elif before.channel is not None and after.channel is None:
                moderator = await _check_voice_audit(guild, discord.AuditLogAction.member_disconnect)
                if moderator:
                    embed = _embed(
                        icon="⚡", label="Member Disconnected", color=COLOR_VOICE_MOD,
                        desc=f"{member.mention} was disconnected from {before.channel.mention}",
                    )
                    embed.add_field(name="Disconnected by", value=moderator.mention, inline=True)
                else:
                    embed = _embed(
                        icon="\U0001F507", label="Left Voice", color=COLOR_VOICE,
                        desc=f"{member.mention} left {before.channel.mention}",
                    )
                await post_to_server_log_channel(guild, embed)

            else:
                moderator = await _check_voice_audit(guild, discord.AuditLogAction.member_move)
                if moderator:
                    embed = _embed(
                        icon="⚡", label="Member Moved", color=COLOR_VOICE_MOD,
                        desc=f"{member.mention} was moved by a moderator",
                    )
                    embed.add_field(name="Moved by", value=moderator.mention, inline=True)
                else:
                    embed = _embed(
                        icon="\U0001F500", label="Switched Channel", color=COLOR_VOICE,
                        desc=f"{member.mention} switched voice channels",
                    )
                embed.add_field(name="From", value=before.channel.mention, inline=True)
                embed.add_field(name="To", value=after.channel.mention, inline=True)
                await post_to_server_log_channel(guild, embed)
            return

        # --- Same channel: server mute / deafen changes -----------------------
        if before.mute != after.mute:
            if after.mute:
                embed = _embed(
                    icon="\U0001F507", label="Server Muted", color=COLOR_VOICE_MOD,
                    desc=f"{member.mention} was server muted in {after.channel.mention}",
                )
            else:
                embed = _embed(
                    icon="\U0001F50A", label="Server Unmuted", color=COLOR_VOICE,
                    desc=f"{member.mention} was server unmuted in {after.channel.mention}",
                )
            await post_to_server_log_channel(guild, embed)

        if before.deaf != after.deaf:
            if after.deaf:
                embed = _embed(
                    icon="\U0001F515", label="Server Deafened", color=COLOR_VOICE_MOD,
                    desc=f"{member.mention} was server deafened in {after.channel.mention}",
                )
            else:
                embed = _embed(
                    icon="\U0001F514", label="Server Undeafened", color=COLOR_VOICE,
                    desc=f"{member.mention} was server undeafened in {after.channel.mention}",
                )
            await post_to_server_log_channel(guild, embed)

    # --- Channels -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        _, moderator = await _get_audit_entry(channel.guild, channel.id, discord.AuditLogAction.channel_create)
        embed = _embed(icon="➕", label="Channel Created", color=COLOR_CHANNEL)
        embed.add_field(name="Name", value=channel.mention, inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        if moderator:
            embed.add_field(name="Created by", value=moderator.mention, inline=True)
        await post_to_server_log_channel(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        _, moderator = await _get_audit_entry(channel.guild, channel.id, discord.AuditLogAction.channel_delete)
        embed = _embed(icon="➖", label="Channel Deleted", color=COLOR_DELETE)
        embed.add_field(name="Name", value=f"`#{channel.name}`", inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        embed.add_field(name="ID", value=f"`{channel.id}`", inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        if moderator:
            embed.add_field(name="Deleted by", value=moderator.mention, inline=True)
        await post_to_server_log_channel(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        changes: list[tuple[str, str, str]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}`", f"`{after.name}`"))
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(("Topic", _short(getattr(before, "topic", None) or "*None*", 200),
                            _short(getattr(after, "topic", None) or "*None*", 200)))
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(("Slowmode", f"{getattr(before, 'slowmode_delay', 0)}s",
                            f"{getattr(after, 'slowmode_delay', 0)}s"))
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(("NSFW", str(getattr(before, "nsfw", False)),
                            str(getattr(after, "nsfw", False))))
        if not changes:
            return

        _, moderator = await _get_audit_entry(after.guild, after.id, discord.AuditLogAction.channel_update)
        embed = _embed(icon="✏️", label="Channel Updated", color=COLOR_CHANNEL)
        embed.add_field(name="Channel", value=after.mention, inline=False)
        if moderator:
            embed.add_field(name="Updated by", value=moderator.mention, inline=True)
        for name, old, new in changes:
            embed.add_field(name=name, value=f"{old} → {new}", inline=False)
        await post_to_server_log_channel(after.guild, embed)

    # --- Roles ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        _, moderator = await _get_audit_entry(role.guild, role.id, discord.AuditLogAction.role_create)
        embed = _embed(icon="\U0001F6E1️", label="Role Created", color=COLOR_ROLE)
        embed.add_field(name="Name", value=role.mention, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        if moderator:
            embed.add_field(name="Created by", value=moderator.mention, inline=True)
        await post_to_server_log_channel(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        _, moderator = await _get_audit_entry(role.guild, role.id, discord.AuditLogAction.role_delete)
        embed = _embed(icon="\U0001F6E1️", label="Role Deleted", color=COLOR_DELETE)
        embed.add_field(name="Name", value=f"@{role.name}", inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        if moderator:
            embed.add_field(name="Deleted by", value=moderator.mention, inline=True)
        await post_to_server_log_channel(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[tuple[str, str, str]] = []
        if before.name != after.name:
            changes.append(("Name", before.name, after.name))
        if before.color != after.color:
            changes.append(("Color", str(before.color), str(after.color)))
        if before.hoist != after.hoist:
            changes.append(("Hoisted", str(before.hoist), str(after.hoist)))
        if before.mentionable != after.mentionable:
            changes.append(("Mentionable", str(before.mentionable), str(after.mentionable)))
        if not changes:
            return

        _, moderator = await _get_audit_entry(after.guild, after.id, discord.AuditLogAction.role_update)
        embed = _embed(icon="\U0001F6E1️", label="Role Updated", color=COLOR_ROLE)
        embed.add_field(name="Role", value=after.mention, inline=False)
        if moderator:
            embed.add_field(name="Updated by", value=moderator.mention, inline=True)
        for name, old, new in changes:
            embed.add_field(name=name, value=f"{old} → {new}", inline=False)
        await post_to_server_log_channel(after.guild, embed)

    # --- Invites --------------------------------------------------------------

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = _embed(icon="\U0001F517", label="Invite Created", color=COLOR_INVITE)
        embed.add_field(name="Code", value=f"[{invite.code}]({invite.url})", inline=True)
        if invite.inviter:
            embed.add_field(name="Created by", value=invite.inviter.mention, inline=True)
        if invite.channel:
            embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        if invite.max_age == 0:
            expires = "Never"
        elif invite.max_age < 3600:
            expires = f"{invite.max_age // 60}m"
        else:
            expires = f"{invite.max_age // 3600}h"
        embed.add_field(name="Expires", value=expires, inline=True)
        embed.add_field(
            name="Max uses",
            value="Unlimited" if invite.max_uses == 0 else str(invite.max_uses),
            inline=True,
        )
        await post_to_server_log_channel(invite.guild, embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = _embed(icon="\U0001F517", label="Invite Deleted", color=COLOR_DELETE)
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        if invite.channel:
            embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        await post_to_server_log_channel(invite.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerLogs(bot))
