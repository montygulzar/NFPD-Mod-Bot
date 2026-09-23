"""Full server event logging - mirrors Sapphire's audit log output.

Covers: messages (delete/edit/bulk delete), members (join/leave/ban/unban/nickname/roles),
voice state (join/move/leave), channels (create/delete/edit), roles (create/delete/edit),
and invites (create/delete).

Uses post_to_server_log_channel which routes to the guild's dedicated server-log channel
if configured, falling back to the mod-log channel so existing setups keep working.
"""
from __future__ import annotations

import discord
from discord.ext import commands

import embeds as embeds_module
from config import BRAND_NAME
from modlog import post_to_server_log_channel

COLOR_JOIN     = 0x3BA55D
COLOR_LEAVE    = 0x747F8D
COLOR_DELETE   = 0xD93A3A
COLOR_EDIT     = 0xF5A524
COLOR_VOICE    = 0x5865F2
COLOR_ROLE     = 0xEB459E
COLOR_CHANNEL  = 0x57F287
COLOR_BAN      = 0xA32828
COLOR_UNBAN    = 0x3BA55D
COLOR_INVITE   = 0x9B59B6
COLOR_NICKNAME = 0xFEE75C


def _base(title: str, color: int, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
    return embed


def _author(embed: discord.Embed, user: discord.abc.User | discord.Member) -> discord.Embed:
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    return embed


def _short(text: str | None, limit: int = 1024) -> str:
    if not text or not text.strip():
        return "*empty*"
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


class ServerLogs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -----------------------------------------------------------------------
    # Messages
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        embed = _base("\U0001F5D1  Message Deleted", COLOR_DELETE)
        _author(embed, message.author)
        embed.add_field(name="Author", value=f"{message.author.mention} `{message.author.id}`", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
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
        non_bot = [m for m in messages if not m.author.bot]
        embed = _base("\U0001F5D1  Bulk Message Delete", COLOR_DELETE)
        embed.add_field(name="Channel", value=messages[0].channel.mention, inline=True)
        embed.add_field(name="User messages removed", value=str(len(non_bot)), inline=True)
        embed.add_field(name="Total removed", value=str(len(messages)), inline=True)
        await post_to_server_log_channel(messages[0].guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return

        embed = _base("\u270F  Message Edited", COLOR_EDIT)
        _author(embed, before.author)
        embed.add_field(name="Author", value=f"{before.author.mention} `{before.author.id}`", inline=True)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump", value=f"[View message]({after.jump_url})", inline=True)
        embed.add_field(name="Before", value=_short(before.content, 512), inline=False)
        embed.add_field(name="After", value=_short(after.content, 512), inline=False)
        await post_to_server_log_channel(before.guild, embed)

    # -----------------------------------------------------------------------
    # Members
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        age = discord.utils.utcnow() - member.created_at
        age_days = age.days

        embed = _base("\U0001F49A  Member Joined", COLOR_JOIN)
        _author(embed, member)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(
            name="Account age",
            value=f"{discord.utils.format_dt(member.created_at, 'R')}\n({age_days}d old)",
            inline=True,
        )
        embed.add_field(name="Member #", value=str(member.guild.member_count), inline=True)
        if age_days < 7:
            embed.add_field(
                name="\u26A0  New account warning",
                value=f"This account is only **{age_days} day(s)** old.",
                inline=False,
            )
        await post_to_server_log_channel(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        embed = _base("\U0001F4A8  Member Left", COLOR_LEAVE)
        _author(embed, member)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        if roles:
            # Unclamped this overflows the 1024-char field limit on role-heavy members,
            # and Discord then rejects the entire embed - losing the log entry outright.
            embed.add_field(name="Roles", value=_short(", ".join(roles)), inline=False)
        await post_to_server_log_channel(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        reason, moderator = await _get_audit_entry(guild, user.id, discord.AuditLogAction.ban)
        embed = _base("\U0001F6D1  Member Banned", COLOR_BAN)
        _author(embed, user)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention}\n`{user.id}`", inline=True)
        if moderator:
            embed.add_field(name="Banned by", value=moderator.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=_short(reason), inline=False)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        reason, moderator = await _get_audit_entry(guild, user.id, discord.AuditLogAction.unban)
        embed = _base("\U0001F513  Member Unbanned", COLOR_UNBAN)
        _author(embed, user)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention}\n`{user.id}`", inline=True)
        if moderator:
            embed.add_field(name="Unbanned by", value=moderator.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=_short(reason), inline=False)
        await post_to_server_log_channel(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        guild = before.guild

        if before.nick != after.nick:
            embed = _base("\U0001F3F7  Nickname Changed", COLOR_NICKNAME)
            _author(embed, after)
            embed.add_field(name="User", value=f"{after.mention}\n`{after.id}`", inline=True)
            embed.add_field(name="Before", value=before.nick or "*none*", inline=True)
            embed.add_field(name="After", value=after.nick or "*none*", inline=True)
            await post_to_server_log_channel(guild, embed)

        added = [r for r in after.roles if r not in before.roles and r != guild.default_role]
        removed = [r for r in before.roles if r not in after.roles and r != guild.default_role]
        if added or removed:
            embed = _base("\U0001F6E1  Member Roles Updated", COLOR_ROLE)
            _author(embed, after)
            embed.add_field(name="User", value=f"{after.mention}\n`{after.id}`", inline=True)
            if added:
                embed.add_field(name="Added", value=_short(", ".join(r.mention for r in added)), inline=False)
            if removed:
                embed.add_field(name="Removed", value=_short(", ".join(r.mention for r in removed)), inline=False)
            await post_to_server_log_channel(guild, embed)

    # -----------------------------------------------------------------------
    # Voice
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return

        if before.channel is None:
            embed = _base("\U0001F50A  Joined Voice", COLOR_VOICE)
            embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
            embed.add_field(name="Channel", value=after.channel.mention, inline=True)
        elif after.channel is None:
            embed = _base("\U0001F507  Left Voice", COLOR_VOICE)
            embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
            embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        else:
            embed = _base("\U0001F500  Moved Voice Channel", COLOR_VOICE)
            embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
            embed.add_field(name="From", value=before.channel.mention, inline=True)
            embed.add_field(name="To", value=after.channel.mention, inline=True)

        _author(embed, member)
        await post_to_server_log_channel(member.guild, embed)

    # -----------------------------------------------------------------------
    # Channels
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        embed = _base("\u2795  Channel Created", COLOR_CHANNEL)
        embed.add_field(name="Name", value=channel.mention, inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        await post_to_server_log_channel(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        embed = _base("\u2796  Channel Deleted", COLOR_DELETE)
        embed.add_field(name="Name", value=f"`#{channel.name}`", inline=True)
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title(), inline=True)
        embed.add_field(name="ID", value=f"`{channel.id}`", inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
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
            changes.append(("Topic", _short(before.topic or "*none*", 200), _short(after.topic or "*none*", 200)))
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(("Slowmode", f"{before.slowmode_delay}s", f"{after.slowmode_delay}s"))
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(("NSFW", str(before.nsfw), str(after.nsfw)))
        if not changes:
            return

        embed = _base("\u270F  Channel Updated", COLOR_CHANNEL)
        embed.add_field(name="Channel", value=after.mention, inline=False)
        for name, old, new in changes:
            embed.add_field(name=name, value=f"{old} - {new}", inline=False)
        await post_to_server_log_channel(after.guild, embed)

    # -----------------------------------------------------------------------
    # Roles
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        embed = _base("\U0001F6E1  Role Created", COLOR_ROLE)
        embed.add_field(name="Name", value=role.mention, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        await post_to_server_log_channel(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        embed = _base("\U0001F6E1  Role Deleted", COLOR_DELETE)
        embed.add_field(name="Name", value=f"@{role.name}", inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
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

        embed = _base("\U0001F6E1  Role Updated", COLOR_ROLE)
        embed.add_field(name="Role", value=after.mention, inline=False)
        for name, old, new in changes:
            embed.add_field(name=name, value=f"{old} - {new}", inline=False)
        await post_to_server_log_channel(after.guild, embed)

    # -----------------------------------------------------------------------
    # Invites
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = _base("\U0001F517  Invite Created", COLOR_INVITE)
        embed.add_field(name="Code", value=f"[{invite.code}]({invite.url})", inline=True)
        if invite.inviter:
            embed.add_field(name="Created by", value=f"{invite.inviter.mention}", inline=True)
        if invite.channel:
            embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        embed.add_field(name="Expires", value="Never" if invite.max_age == 0 else f"{invite.max_age // 3600}h", inline=True)
        embed.add_field(name="Max uses", value="Unlimited" if invite.max_uses == 0 else str(invite.max_uses), inline=True)
        await post_to_server_log_channel(invite.guild, embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = _base("\U0001F517  Invite Deleted", COLOR_DELETE)
        embed.add_field(name="Code", value=invite.code, inline=True)
        if invite.channel:
            embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        await post_to_server_log_channel(invite.guild, embed)


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------

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


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerLogs(bot))
