from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    add_temp_ban,
    get_guild_settings,
    get_suspended_member,
    get_warn_count,
    remove_suspended_member,
    remove_temp_ban,
    save_suspended_member,
)
from embeds import audit_reason, build_ban_dm_embed, build_dm_notice_embed, build_notice_embed
from guards import has_tier, refusal_reason
from modlog import announce_case, record_case, try_dm
from views import BanAppealView

MAX_TIMEOUT_MINUTES = 40320  # Discord's own cap: 28 days
MAX_TEMPBAN_MINUTES = 525600  # 1 year


async def notify_member(member: discord.Member, action_type: str, guild_name: str, reason: str) -> None:
    await try_dm(member, build_dm_notice_embed(action_type, guild_name, reason))


async def perform_or_report(ctx: commands.Context, action_label: str, coroutine) -> bool:
    """Run a moderation action; on failure, tell the moderator plainly rather than letting a
    generic error surface after the member may already have been DMed that it succeeded."""
    try:
        await coroutine
        return True
    except discord.HTTPException as error:
        await ctx.send(
            embed=build_notice_embed(
                f"Sent the notice, but the {action_label} itself failed: `{error}`. "
                "Check my role position and permissions - nothing was recorded.",
                success=False,
            )
        )
        return False


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def escalate_if_needed(self, ctx: commands.Context, member: discord.Member, warn_count: int) -> None:
        settings = await get_guild_settings(ctx.guild.id)
        reason = f"Automatic action after reaching {warn_count} warns"

        if settings["warn_ban_threshold"] == warn_count:
            action_type = "ban"
        elif settings["warn_kick_threshold"] == warn_count:
            action_type = "kick"
        elif settings["warn_mute_threshold"] == warn_count:
            action_type = "mute"
        else:
            return

        try:
            if action_type == "ban":
                await member.ban(reason=reason)
            elif action_type == "kick":
                await member.kick(reason=reason)
            else:
                mute_minutes = max(1, settings["warn_mute_minutes"] or 60)
                until = discord.utils.utcnow() + timedelta(minutes=mute_minutes)
                await member.timeout(until, reason=reason)
        except discord.HTTPException:
            await ctx.send(
                embed=build_notice_embed(
                    f"{member.mention} hit the warn threshold for an automatic {action_type}, "
                    "but I couldn't carry it out. Check my permissions and role position.",
                    success=False,
                )
            )
            return

        await announce_case(ctx, member, action_type, reason, moderator=self.bot.user)

    @commands.hybrid_command(name="kick", description="Kick a member from this server")
    @app_commands.describe(member="The member to kick", reason="Why they're being kicked")
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()
        await notify_member(member, "kick", ctx.guild.name, reason)
        succeeded = await perform_or_report(
            ctx, "kick", member.kick(reason=audit_reason(ctx.author, "Kick", reason))
        )
        if succeeded:
            await announce_case(ctx, member, "kick", reason)

    @commands.hybrid_command(name="ban", description="Permanently ban a member from this server")
    @app_commands.describe(member="The member to ban", reason="Why they're being banned")
    @commands.guild_only()
    @has_tier("ban_perm")
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()
        # DM the ban notice with the appeal button before removing them from the server.
        await try_dm(member, build_ban_dm_embed(reason), BanAppealView())
        succeeded = await perform_or_report(
            ctx, "ban", member.ban(reason=audit_reason(ctx.author, "Ban", reason))
        )
        if succeeded:
            await announce_case(ctx, member, "ban", reason)

    @commands.hybrid_command(name="tempban", description="Ban a member and automatically unban them later")
    @app_commands.describe(
        member="The member to ban",
        duration_minutes="How long the ban lasts, in minutes",
        reason="Why they're being banned",
    )
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, 525600],
        *,
        reason: str = "No reason provided",
    ):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()
        unban_at = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
        # Show the expiry time in the DM so they know exactly when the ban lifts.
        expiry_str = discord.utils.format_dt(unban_at, style="F")
        await try_dm(member, build_ban_dm_embed(reason, unban_at=expiry_str), BanAppealView())

        succeeded = await perform_or_report(
            ctx, "ban", member.ban(reason=audit_reason(ctx.author, "Tempban", reason))
        )
        if not succeeded:
            return

        await add_temp_ban(ctx.guild.id, member.id, unban_at)
        embed = await record_case(ctx.guild, member, ctx.author, "ban", reason)
        embed.add_field(name="Expires", value=discord.utils.format_dt(unban_at, style="R"), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unban", description="Unban a user from this server")
    @app_commands.describe(user="The user to unban", reason="Why they're being unbanned")
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided"):
        await ctx.defer()
        try:
            await ctx.guild.unban(user, reason=audit_reason(ctx.author, "Unban", reason))
        except discord.NotFound:
            await ctx.send(embed=build_notice_embed(f"**{user}** isn't banned here.", success=False))
            return
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Couldn't unban **{user}**: `{error}`", success=False))
            return

        await remove_temp_ban(ctx.guild.id, user.id)
        await announce_case(ctx, user, "unban", reason)

    @commands.hybrid_command(name="warn", description="Warn a member")
    @app_commands.describe(member="The member to warn", reason="Why they're being warned")
    @commands.guild_only()
    @has_tier("mod")
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id, check_hierarchy=False)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()
        await notify_member(member, "warn", ctx.guild.name, reason)
        await announce_case(ctx, member, "warn", reason)

        warn_count = await get_warn_count(ctx.guild.id, member.id)
        await self.escalate_if_needed(ctx, member, warn_count)

    @commands.hybrid_command(name="mute", description="Timeout a member for a set duration")
    @app_commands.describe(
        member="The member to mute",
        duration_minutes="How long to mute for, in minutes (max 40320 = 28 days)",
        reason="Why they're being muted",
    )
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(moderate_members=True)
    async def mute(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, 40320],
        *,
        reason: str = "No reason provided",
    ):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()
        until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
        succeeded = await perform_or_report(
            ctx, "mute", member.timeout(until, reason=audit_reason(ctx.author, "Mute", reason))
        )
        if not succeeded:
            return

        await notify_member(member, "mute", ctx.guild.name, reason)
        embed = await record_case(ctx.guild, member, ctx.author, "mute", reason)
        embed.add_field(name="Expires", value=discord.utils.format_dt(until, style="R"), inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unmute", description="Remove an active timeout from a member")
    @app_commands.describe(member="The member to unmute", reason="Why they're being unmuted")
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(moderate_members=True)
    async def unmute(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if member.timed_out_until is None:
            await ctx.send(embed=build_notice_embed(f"{member.mention} isn't currently muted.", success=False))
            return

        await ctx.defer()
        try:
            await member.timeout(None, reason=audit_reason(ctx.author, "Unmute", reason))
        except discord.HTTPException as error:
            await ctx.send(
                embed=build_notice_embed(
                    f"Couldn't unmute {member.mention}: `{error}`. "
                    "Check my permissions and role position.",
                    success=False,
                )
            )
            return
        await announce_case(ctx, member, "unmute", reason)


    @commands.hybrid_command(name="suspend", description="Remove all roles and assign the suspended role")
    @app_commands.describe(member="The member to suspend", reason="Why they're being suspended")
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(manage_roles=True)
    async def suspend(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        refusal = refusal_reason(ctx.author, member, self.bot.user.id)
        if refusal:
            await ctx.send(embed=build_notice_embed(refusal, success=False))
            return

        await ctx.defer()

        settings = await get_guild_settings(ctx.guild.id)
        suspended_role_id = settings.get("suspended_role_id")
        if not suspended_role_id:
            await ctx.send(embed=build_notice_embed(
                "No suspended role configured. Use `/setsuspendedrole` first.", success=False,
            ))
            return

        suspended_role = ctx.guild.get_role(suspended_role_id)
        if not suspended_role:
            await ctx.send(embed=build_notice_embed(
                "The configured suspended role no longer exists. Use `/setsuspendedrole` to set a new one.",
                success=False,
            ))
            return

        if await get_suspended_member(ctx.guild.id, member.id) is not None:
            await ctx.send(embed=build_notice_embed(
                f"{member.mention} is already suspended.", success=False,
            ))
            return

        bot_top = ctx.guild.me.top_role
        removable = [
            r for r in member.roles
            if r != ctx.guild.default_role
            and not r.managed
            and r < bot_top
            and r != suspended_role
        ]

        await save_suspended_member(ctx.guild.id, member.id, [r.id for r in removable])

        try:
            await member.add_roles(suspended_role, reason=audit_reason(ctx.author, "Suspend", reason))
            if removable:
                await member.remove_roles(*removable, reason=audit_reason(ctx.author, "Suspend", reason))
        except discord.HTTPException as error:
            await remove_suspended_member(ctx.guild.id, member.id)
            await ctx.send(embed=build_notice_embed(
                f"Failed to suspend {member.mention}: `{error}`", success=False,
            ))
            return

        await notify_member(member, "suspend", ctx.guild.name, reason)
        embed = await record_case(ctx.guild, member, ctx.author, "suspend", reason)
        embed.add_field(name="Roles removed", value=str(len(removable)), inline=True)
        kept = [r for r in member.roles if r != ctx.guild.default_role and r not in removable and r != suspended_role]
        if kept:
            embed.add_field(name="Kept (managed)", value=", ".join(r.mention for r in kept), inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unsuspend", description="Restore a suspended member's roles")
    @app_commands.describe(member="The member to unsuspend", reason="Why they're being unsuspended")
    @commands.guild_only()
    @has_tier("mod")
    @commands.bot_has_permissions(manage_roles=True)
    async def unsuspend(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await ctx.defer()

        saved_role_ids = await get_suspended_member(ctx.guild.id, member.id)
        if saved_role_ids is None:
            await ctx.send(embed=build_notice_embed(
                f"{member.mention} is not currently suspended.", success=False,
            ))
            return

        settings = await get_guild_settings(ctx.guild.id)
        suspended_role_id = settings.get("suspended_role_id")
        suspended_role = ctx.guild.get_role(suspended_role_id) if suspended_role_id else None

        bot_top = ctx.guild.me.top_role
        roles_to_add = [
            ctx.guild.get_role(rid) for rid in saved_role_ids
        ]
        roles_to_add = [r for r in roles_to_add if r is not None and r < bot_top]

        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=audit_reason(ctx.author, "Unsuspend", reason))
            if suspended_role and suspended_role in member.roles:
                await member.remove_roles(suspended_role, reason=audit_reason(ctx.author, "Unsuspend", reason))
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(
                f"Partially restored roles for {member.mention}: `{error}`", success=False,
            ))

        await remove_suspended_member(ctx.guild.id, member.id)
        embed = await record_case(ctx.guild, member, ctx.author, "unsuspend", reason)
        embed.add_field(name="Roles restored", value=str(len(roles_to_add)), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
