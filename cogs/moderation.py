from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database import add_temp_ban, get_guild_settings, get_warn_count, remove_temp_ban
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
