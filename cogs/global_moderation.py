from datetime import timedelta
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import APPROVED_GUILD_IDS, GLOBAL_ACTION_EXEMPT_GUILD_IDS
from embeds import audit_reason, build_ban_dm_embed, build_dm_notice_embed, build_notice_embed, build_summary_embed
from guards import from_approved_guild, has_tier, highest_tier_anywhere, is_protected
from modlog import record_case, try_dm
from views import BanAppealView
from views import ConfirmView, build_confirm_prompt

MAX_TIMEOUT_MINUTES = 40320  # Discord's own cap: 28 days

GuildAction = Callable[[discord.Guild, Optional[discord.Member]], Awaitable[None]]


def target_guilds(bot: commands.Bot) -> list[discord.Guild]:
    """Servers a global action is allowed to touch.

    Falls back to every server if no allowlist is set. Guilds in
    GLOBAL_ACTION_EXEMPT_GUILD_IDS are always excluded — they receive no global
    actions so that (for example) a globally banned user can still access the
    Appeals server.
    """
    if APPROVED_GUILD_IDS:
        guilds = [g for g in bot.guilds if g.id in APPROVED_GUILD_IDS]
    else:
        guilds = list(bot.guilds)
    if GLOBAL_ACTION_EXEMPT_GUILD_IDS:
        guilds = [g for g in guilds if g.id not in GLOBAL_ACTION_EXEMPT_GUILD_IDS]
    return guilds


async def notify_user(user: discord.User, action_type: str, reason: str) -> None:
    await try_dm(user, build_dm_notice_embed(action_type, "all servers", reason))


async def notify_global_ban(user: discord.User, reason: str) -> None:
    """Send the professional NFPD ban DM with the appeal button for global bans."""
    await try_dm(user, build_ban_dm_embed(reason, is_global=True), BanAppealView())


async def refuse_protected(ctx: commands.Context, user: discord.User) -> bool:
    """Global actions bypass per-server role hierarchy entirely, so this is the only
    thing standing between a rogue global moderator and anyone they please.

    Discord role position is meaningless across servers, so the tier ladder is the
    comparison used here instead of the per-guild hierarchy check.
    """
    if user.id == ctx.author.id:
        refusal = "You can't use that on yourself."
    elif ctx.bot.user is not None and user.id == ctx.bot.user.id:
        refusal = "You can't use that on me."
    elif is_protected(user.id):
        refusal = f"**{user}** is on the protected list and can't be moderated."
    else:
        actor_tier = highest_tier_anywhere(ctx.bot, ctx.author)
        target_tier = highest_tier_anywhere(ctx.bot, user)
        if target_tier is not None and actor_tier is not None and target_tier >= actor_tier:
            refusal = "You can't take a global action against someone at your tier or above."
        else:
            return False

    await ctx.send(embed=build_notice_embed(refusal, success=False))
    return True


async def request_confirmation(ctx: commands.Context, description: str) -> bool:
    view = ConfirmView(author_id=ctx.author.id)
    view.message = await ctx.send(embed=build_confirm_prompt(description), view=view)
    await view.wait()
    return bool(view.confirmed)


class GlobalModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def apply_everywhere(
        self,
        ctx: commands.Context,
        user: discord.User,
        action_type: str,
        reason: str,
        perform: GuildAction,
        *,
        member_only: bool,
    ) -> None:
        """Run one action across every guild, recording a case per guild it succeeded in."""
        affected, failed = [], []

        for guild in target_guilds(self.bot):
            member = guild.get_member(user.id)
            if member_only and member is None:
                continue

            try:
                await perform(guild, member)
            except discord.NotFound:
                continue  # nothing to undo in this guild
            except (discord.Forbidden, discord.HTTPException):
                failed.append(guild.name)
                continue

            await record_case(guild, user, ctx.author, action_type, reason)
            affected.append(guild.name)

        await ctx.send(embed=build_summary_embed(action_type, user, affected, failed))

    @commands.hybrid_command(name="globalkick", description="Kick a user from every server the bot shares with them")
    @app_commands.describe(user="The user to kick everywhere", reason="Why they're being kicked")
    @commands.guild_only()
    @has_tier("management")
    @from_approved_guild()
    async def globalkick(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided"):
        if await refuse_protected(ctx, user):
            return
        if not await request_confirmation(ctx, f"Kick **{user}** from every server they share with this bot?"):
            await ctx.send(embed=build_notice_embed("Global kick cancelled.", success=False))
            return

        await notify_user(user, "global_kick", reason)
        reason_text = audit_reason(ctx.author, "Global kick", reason)

        await self.apply_everywhere(
            ctx, user, "global_kick", reason,
            lambda guild, member: member.kick(reason=reason_text),
            member_only=True,
        )

    @commands.hybrid_command(name="globalban", description="Ban a user from every server the bot is in")
    @app_commands.describe(user="The user to ban everywhere", reason="Why they're being banned")
    @commands.guild_only()
    @has_tier("management")
    @from_approved_guild()
    async def globalban(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided"):
        if await refuse_protected(ctx, user):
            return
        if not await request_confirmation(ctx, f"Ban **{user}** from **every server** this bot is in?"):
            await ctx.send(embed=build_notice_embed("Global ban cancelled.", success=False))
            return

        await notify_global_ban(user, reason)
        reason_text = audit_reason(ctx.author, "Global ban", reason)

        await self.apply_everywhere(
            ctx, user, "global_ban", reason,
            lambda guild, member: guild.ban(user, reason=reason_text, delete_message_seconds=0),
            member_only=False,
        )

    @commands.hybrid_command(name="globalunban", description="Unban a user from every server the bot is in")
    @app_commands.describe(user="The user to unban everywhere", reason="Why they're being unbanned")
    @commands.guild_only()
    @has_tier("management")
    @from_approved_guild()
    async def globalunban(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided"):
        if await refuse_protected(ctx, user):
            return
        await ctx.defer()
        reason_text = audit_reason(ctx.author, "Global unban", reason)

        await self.apply_everywhere(
            ctx, user, "global_unban", reason,
            lambda guild, member: guild.unban(user, reason=reason_text),
            member_only=False,
        )

    @commands.hybrid_command(name="globalmute", description="Timeout a user in every server the bot shares with them")
    @app_commands.describe(
        user="The user to mute everywhere",
        duration_minutes="How long to mute for, in minutes (max 40320 = 28 days)",
        reason="Why they're being muted",
    )
    @commands.guild_only()
    @has_tier("management")
    @from_approved_guild()
    async def globalmute(
        self,
        ctx: commands.Context,
        user: discord.User,
        duration_minutes: app_commands.Range[int, 1, 40320],
        *,
        reason: str = "No reason provided",
    ):
        if await refuse_protected(ctx, user):
            return
        if not await request_confirmation(
            ctx, f"Mute **{user}** for {duration_minutes} minutes in every shared server?"
        ):
            await ctx.send(embed=build_notice_embed("Global mute cancelled.", success=False))
            return

        await notify_user(user, "global_mute", reason)
        until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
        reason_text = audit_reason(ctx.author, "Global mute", reason)

        await self.apply_everywhere(
            ctx, user, "global_mute", reason,
            lambda guild, member: member.timeout(until, reason=reason_text),
            member_only=True,
        )

    @commands.hybrid_command(name="globalunmute", description="Clear a user's timeout in every shared server")
    @app_commands.describe(user="The user to unmute everywhere", reason="Why they're being unmuted")
    @commands.guild_only()
    @has_tier("management")
    @from_approved_guild()
    async def globalunmute(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided"):
        if await refuse_protected(ctx, user):
            return
        await ctx.defer()
        reason_text = audit_reason(ctx.author, "Global unmute", reason)

        await self.apply_everywhere(
            ctx, user, "global_unmute", reason,
            lambda guild, member: member.timeout(None, reason=reason_text),
            member_only=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalModeration(bot))
