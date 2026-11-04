from datetime import datetime, timezone

import discord
from discord.ext import commands

from database import get_guild_settings
from embeds import base_embed
from modlog import post_to_server_log_channel

ALERT_COLOR = 0xF5A524


class RaidProtection(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return  # A freshly created bot account is not a raid signal.
        settings = await get_guild_settings(member.guild.id)
        minimum_age_hours = settings["raid_min_account_age_hours"]
        if not minimum_age_hours:
            return

        account_age = datetime.now(timezone.utc) - member.created_at
        if account_age.total_seconds() >= minimum_age_hours * 3600:
            return

        embed = base_embed(
            "New Account Alert",
            ALERT_COLOR,
            f"{member.mention} joined with an account under **{minimum_age_hours}h** old.",
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
        await post_to_server_log_channel(member.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidProtection(bot))
