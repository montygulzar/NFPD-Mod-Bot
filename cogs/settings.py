import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_guild_settings,
    get_lockdown_role_ids,
    set_log_channel,
    set_raid_protection,
    set_server_log_channel,
    set_warn_thresholds,
)
from embeds import NEUTRAL_COLOR, base_embed, build_notice_embed
from guards import has_tier
from modlog import _resolve_channel, check_log_channel, check_server_log_channel

MAX_TIMEOUT_MINUTES = 40320  # Discord's own cap on a timeout: 28 days


def describe_threshold(count: int | None, suffix: str = "") -> str:
    return f"{count} warns{suffix}" if count else "Disabled"


async def _channel_display(guild: discord.Guild, channel_id: int | None) -> str:
    if channel_id is None:
        return "Not set"
    channel = await _resolve_channel(guild, channel_id)
    return channel.mention if channel else f"`{channel_id}` *(not found)*"


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="settings", description="Show this server's moderation configuration")
    @commands.guild_only()
    @has_tier("ownership")
    async def settings(self, ctx: commands.Context):
        config = await get_guild_settings(ctx.guild.id)
        lockdown_role_ids = await get_lockdown_role_ids(ctx.guild.id)
        raid_hours = config["raid_min_account_age_hours"]
        mute_minutes = config["warn_mute_minutes"]

        mod_log_display = await _channel_display(ctx.guild, config["log_channel_id"])
        server_log_id = config.get("server_log_channel_id")
        if server_log_id:
            server_log_display = await _channel_display(ctx.guild, server_log_id)
        else:
            server_log_display = f"{mod_log_display} *(same as mod-log)*"

        lockdown_roles = [ctx.guild.get_role(r) for r in lockdown_role_ids if ctx.guild.get_role(r)]
        lockdown_value = (
            ", ".join(r.mention for r in lockdown_roles)
            if lockdown_roles
            else "Not set, falls back to @everyone"
        )

        embed = base_embed(f"Settings  \u2022  {ctx.guild.name}", NEUTRAL_COLOR)
        embed.add_field(name="Mod-log channel", value=mod_log_display, inline=True)
        embed.add_field(name="Server-log channel", value=server_log_display, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer to keep grid tidy
        embed.add_field(name="Lockdown roles", value=lockdown_value, inline=False)
        embed.add_field(
            name="Raid protection",
            value=f"Flag accounts under {raid_hours}h old" if raid_hours else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Warn escalation",
            value=(
                f"Mute at {describe_threshold(config['warn_mute_threshold'], f' for {mute_minutes}m' if mute_minutes else '')}\n"
                f"Kick at {describe_threshold(config['warn_kick_threshold'])}\n"
                f"Ban at {describe_threshold(config['warn_ban_threshold'])}"
            ),
            inline=True,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setlogchannel", description="Set where moderation cases are logged")
    @app_commands.describe(channel="Channel for ban/kick/warn/mute case logs")
    @commands.guild_only()
    @has_tier("ownership")
    async def setlogchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        await set_log_channel(ctx.guild.id, channel.id)
        ok, detail = await check_log_channel(ctx.guild)
        message = f"Mod-log channel set to {channel.mention}."
        if not ok:
            message += f"\n\u26A0 {detail}"
        await ctx.send(embed=build_notice_embed(message, success=ok))

    @commands.hybrid_command(
        name="setserverlogchannel",
        description="Set where server events are logged (message edits/deletes, joins, voice, etc.)",
    )
    @app_commands.describe(channel="Channel for server event logs, or leave blank to use the mod-log channel")
    @commands.guild_only()
    @has_tier("ownership")
    async def setserverlogchannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ):
        await set_server_log_channel(ctx.guild.id, channel.id if channel else None)
        if channel is None:
            await ctx.send(embed=build_notice_embed("Server-log channel cleared. Server events will post to the mod-log channel."))
            return

        ok, detail = await check_server_log_channel(ctx.guild)
        message = f"Server-log channel set to {channel.mention}."
        if not ok:
            message += f"\n\u26A0 {detail}"
        await ctx.send(embed=build_notice_embed(message, success=ok))

    @commands.hybrid_command(name="testlog", description="Send a test message to the configured mod-log channel")
    @commands.guild_only()
    @has_tier("ownership")
    async def testlog(self, ctx: commands.Context):
        ok, detail = await check_log_channel(ctx.guild)
        if not ok:
            await ctx.send(embed=build_notice_embed(detail, success=False))
            return

        config = await get_guild_settings(ctx.guild.id)
        channel = await _resolve_channel(ctx.guild, config["log_channel_id"])
        if channel is None:
            await ctx.send(embed=build_notice_embed("Could not resolve the log channel. Try /setlogchannel again.", success=False))
            return

        try:
            await channel.send(embed=base_embed("Test Message", NEUTRAL_COLOR, "If you can see this, mod-log is working."))
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Channel looked reachable, but sending failed: `{error}`", success=False))
            return

        await ctx.send(embed=build_notice_embed(f"Test message sent to {channel.mention}."))

    @commands.hybrid_command(name="testserverlog", description="Send a test message to the server-log channel")
    @commands.guild_only()
    @has_tier("ownership")
    async def testserverlog(self, ctx: commands.Context):
        ok, detail = await check_server_log_channel(ctx.guild)
        if not ok:
            await ctx.send(embed=build_notice_embed(detail, success=False))
            return

        config = await get_guild_settings(ctx.guild.id)
        channel_id = config.get("server_log_channel_id") or config.get("log_channel_id")
        channel = await _resolve_channel(ctx.guild, channel_id)
        if channel is None:
            await ctx.send(embed=build_notice_embed("Could not resolve the server-log channel.", success=False))
            return

        try:
            await channel.send(embed=base_embed("Test Message", NEUTRAL_COLOR, "If you can see this, server-log is working."))
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Sending failed: `{error}`", success=False))
            return

        await ctx.send(embed=build_notice_embed(f"Test message sent to {channel.mention}."))

    @commands.hybrid_command(
        name="setraidprotection",
        description="Flag joins from accounts younger than this many hours (0 disables)",
    )
    @app_commands.describe(minimum_account_age_hours="Minimum account age in hours, or 0 to disable")
    @commands.guild_only()
    @has_tier("ownership")
    async def setraidprotection(self, ctx: commands.Context, minimum_account_age_hours: int):
        if minimum_account_age_hours < 0:
            await ctx.send(embed=build_notice_embed("Minimum account age can't be negative.", success=False))
            return

        await set_raid_protection(ctx.guild.id, minimum_account_age_hours or None)
        if minimum_account_age_hours == 0:
            await ctx.send(embed=build_notice_embed("Raid protection disabled."))
            return

        await ctx.send(
            embed=build_notice_embed(
                f"Accounts younger than **{minimum_account_age_hours}h** will be flagged in the server-log channel."
            )
        )

    @commands.hybrid_command(
        name="setwarnthresholds",
        description="Auto-escalate at set warn counts (0 disables that step)",
    )
    @app_commands.describe(
        mute_at="Warn count that triggers an automatic mute (0 to disable)",
        mute_minutes="How long the automatic mute lasts, in minutes",
        kick_at="Warn count that triggers an automatic kick (0 to disable)",
        ban_at="Warn count that triggers an automatic ban (0 to disable)",
    )
    @commands.guild_only()
    @has_tier("ownership")
    async def setwarnthresholds(
        self,
        ctx: commands.Context,
        mute_at: int = 0,
        mute_minutes: int = 60,
        kick_at: int = 0,
        ban_at: int = 0,
    ):
        # A negative value is filtered out of `active` below, but `value or None` would
        # still store it verbatim - leaving a threshold no warn count can ever match.
        if min(mute_at, kick_at, ban_at) < 0:
            await ctx.send(
                embed=build_notice_embed("Warn thresholds can't be negative. Use 0 to disable a step.", success=False)
            )
            return

        active = [value for value in (mute_at, kick_at, ban_at) if value > 0]
        if active != sorted(active) or len(active) != len(set(active)):
            await ctx.send(
                embed=build_notice_embed(
                    "Thresholds must increase in severity, with no ties: mute < kick < ban.", success=False
                )
            )
            return

        # Anything past Discord's timeout cap is stored happily and then fails at the
        # moment it matters, when the automatic mute actually fires.
        if mute_at and not 1 <= mute_minutes <= MAX_TIMEOUT_MINUTES:
            await ctx.send(
                embed=build_notice_embed(
                    f"Mute duration must be between 1 and {MAX_TIMEOUT_MINUTES} minutes "
                    "(Discord caps timeouts at 28 days).",
                    success=False,
                )
            )
            return

        await set_warn_thresholds(
            ctx.guild.id, mute_at or None, mute_minutes if mute_at else None, kick_at or None, ban_at or None
        )

        embed = base_embed("Warn Escalation Updated", NEUTRAL_COLOR)
        embed.add_field(name="Mute", value=describe_threshold(mute_at, f" for {mute_minutes}m"), inline=True)
        embed.add_field(name="Kick", value=describe_threshold(kick_at), inline=True)
        embed.add_field(name="Ban", value=describe_threshold(ban_at), inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
