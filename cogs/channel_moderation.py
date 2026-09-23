from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    add_lockdown_role,
    clear_lockdown_roles,
    get_lockdown_role_ids,
    pop_channel_lock,
    remove_lockdown_role,
    save_channel_lock,
)
from embeds import MUTED_COLOR, NEUTRAL_COLOR, SUCCESS_COLOR, base_embed, build_notice_embed, clamp
from guards import has_tier
from modlog import post_to_log_channel

MAX_SLOWMODE_SECONDS = 21600  # Discord's own cap: 6 hours
MAX_PURGE_MESSAGES = 100

TRISTATE_TO_TEXT = {True: "true", False: "false", None: "none"}
TEXT_TO_TRISTATE = {"true": True, "false": False, "none": None}


class ChannelModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def resolve_lockdown_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """All roles that /lockdown silences. Falls back to [@everyone] if none are configured."""
        role_ids = await get_lockdown_role_ids(guild.id)
        if not role_ids:
            return [guild.default_role]

        roles = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                roles.append(role)

        return roles or [guild.default_role]

    @commands.hybrid_command(name="slowmode", description="Set a channel's slowmode delay")
    @app_commands.describe(
        seconds="Delay between messages in seconds (0 disables, max 21600)",
        channel="Channel to apply to, defaults to the current one",
    )
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        ctx: commands.Context,
        seconds: app_commands.Range[int, 0, 21600],
        channel: Optional[discord.TextChannel] = None,
    ):
        target = channel or ctx.channel
        await target.edit(slowmode_delay=seconds)
        message = (
            f"Slowmode disabled in {target.mention}."
            if seconds == 0
            else f"Slowmode set to **{seconds}s** in {target.mention}."
        )
        await ctx.send(embed=build_notice_embed(message))

    @commands.hybrid_command(name="purge", description="Bulk delete recent messages from a channel")
    @app_commands.describe(
        amount="How many messages to scan and delete (1 to 100)",
        member="Only delete messages from this member",
    )
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(
        self,
        ctx: commands.Context,
        amount: app_commands.Range[int, 1, 100],
        member: Optional[discord.Member] = None,
    ):
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        else:
            await ctx.defer(ephemeral=True)

        message_filter = (lambda m: m.author.id == member.id) if member else (lambda m: True)

        try:
            deleted = await ctx.channel.purge(limit=amount, check=message_filter)
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Couldn't purge messages: `{error}`", success=False))
            return

        scope = f" from {member.mention}" if member else ""
        embed = base_embed(
            "Messages Purged",
            NEUTRAL_COLOR,
            f"Deleted **{len(deleted)}** message(s){scope} in {ctx.channel.mention}.",
        )
        embed.add_field(name="Purged by", value=ctx.author.mention, inline=True)
        # Only warn about the 14-day limit when no member filter was in play;
        # otherwise a shortfall usually just means the filter didn't match, not an age cap.
        if member is None and len(deleted) < amount:
            embed.set_footer(text="Discord can only bulk delete messages under 14 days old.")

        await ctx.send(embed=embed)
        await post_to_log_channel(ctx.guild, embed)

    @commands.hybrid_command(name="lockdown", description="Prevent configured roles from sending messages")
    @app_commands.describe(
        channel="Channel to lock, defaults to the current one",
        reason="Why the channel is being locked",
    )
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_channels=True)
    async def lockdown(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        reason: str = "No reason provided",
    ):
        target = channel or ctx.channel
        locked_roles = await self.resolve_lockdown_roles(ctx.guild)

        # Snapshot each role's current send_messages state before changing anything.
        role_states: dict[int, str] = {}
        for role in locked_roles:
            overwrite = target.overwrites_for(role)
            role_states[role.id] = TRISTATE_TO_TEXT[overwrite.send_messages]

        await save_channel_lock(ctx.guild.id, target.id, role_states)

        failed_roles = []
        for role in locked_roles:
            overwrite = target.overwrites_for(role)
            overwrite.send_messages = False
            try:
                await target.set_permissions(role, overwrite=overwrite, reason=clamp(reason, 512))
            except discord.HTTPException:
                failed_roles.append(role.mention)

        role_list = ", ".join(r.mention for r in locked_roles)
        description = f"{role_list} can no longer post in {target.mention}."
        if failed_roles:
            description += f"\nFailed to apply to: {', '.join(failed_roles)} - check my permissions."

        embed = base_embed("Channel Locked", MUTED_COLOR, description)
        embed.add_field(name="Reason", value=clamp(reason), inline=False)
        embed.add_field(name="Locked by", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)
        await post_to_log_channel(ctx.guild, embed)

    @commands.hybrid_command(name="unlock", description="Restore configured roles' ability to send messages")
    @app_commands.describe(channel="Channel to unlock, defaults to the current one")
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        target = channel or ctx.channel
        saved = await pop_channel_lock(ctx.guild.id, target.id)

        if saved is None:
            # No lock record - fall back to restoring the current lockdown roles to the default.
            locked_roles = await self.resolve_lockdown_roles(ctx.guild)
            for role in locked_roles:
                overwrite = target.overwrites_for(role)
                overwrite.send_messages = None
                try:
                    await target.set_permissions(role, overwrite=overwrite)
                except discord.HTTPException:
                    pass

            embed = base_embed(
                "Channel Unlocked",
                SUCCESS_COLOR,
                f"No lock record found for {target.mention} - reset send permissions to default.",
            )
            embed.add_field(name="Unlocked by", value=ctx.author.mention, inline=True)
            await ctx.send(embed=embed)
            await post_to_log_channel(ctx.guild, embed)
            return

        restored_roles, failed_roles = [], []
        for role_id, state_str in saved.items():
            role = ctx.guild.get_role(role_id)
            if role is None:
                continue  # Role was deleted since the lockdown - nothing to restore.
            overwrite = target.overwrites_for(role)
            overwrite.send_messages = TEXT_TO_TRISTATE.get(state_str)
            try:
                await target.set_permissions(role, overwrite=overwrite)
                restored_roles.append(role.mention)
            except discord.HTTPException:
                failed_roles.append(role.mention)

        description = f"{target.mention} is unlocked."
        if restored_roles:
            description += f"\nRestored: {', '.join(restored_roles)}"
        if failed_roles:
            description += f"\nFailed to restore: {', '.join(failed_roles)}"

        embed = base_embed("Channel Unlocked", SUCCESS_COLOR, description)
        embed.add_field(name="Unlocked by", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)
        await post_to_log_channel(ctx.guild, embed)

    @commands.hybrid_command(name="addlockdownrole", description="Add a role to the lockdown list")
    @app_commands.describe(role="The role that will be silenced during lockdowns")
    @commands.guild_only()
    @has_tier("ownership")
    async def addlockdownrole(self, ctx: commands.Context, role: discord.Role):
        await add_lockdown_role(ctx.guild.id, role.id)
        current_ids = await get_lockdown_role_ids(ctx.guild.id)
        current_roles = [ctx.guild.get_role(r) for r in current_ids if ctx.guild.get_role(r)]
        role_list = ", ".join(r.mention for r in current_roles) if current_roles else "none"
        await ctx.send(embed=build_notice_embed(
            f"{role.mention} added to lockdown roles.\nAll lockdown roles: {role_list}"
        ))

    @commands.hybrid_command(name="removelockdownrole", description="Remove a role from the lockdown list")
    @app_commands.describe(role="The role to remove from the lockdown list")
    @commands.guild_only()
    @has_tier("ownership")
    async def removelockdownrole(self, ctx: commands.Context, role: discord.Role):
        removed = await remove_lockdown_role(ctx.guild.id, role.id)
        if not removed:
            await ctx.send(embed=build_notice_embed(f"{role.mention} was not on the lockdown list.", success=False))
            return
        current_ids = await get_lockdown_role_ids(ctx.guild.id)
        current_roles = [ctx.guild.get_role(r) for r in current_ids if ctx.guild.get_role(r)]
        role_list = ", ".join(r.mention for r in current_roles) if current_roles else "none (falls back to @everyone)"
        await ctx.send(embed=build_notice_embed(
            f"{role.mention} removed from lockdown roles.\nRemaining: {role_list}"
        ))

    @commands.hybrid_command(name="clearlockdownroles", description="Clear all lockdown roles (resets to @everyone)")
    @commands.guild_only()
    @has_tier("ownership")
    async def clearlockdownroles(self, ctx: commands.Context):
        await clear_lockdown_roles(ctx.guild.id)
        await ctx.send(embed=build_notice_embed("All lockdown roles cleared. /lockdown now silences @everyone."))


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelModeration(bot))
