import discord
from discord.ext import commands

from config import APPROVED_GUILD_IDS
from embeds import NEUTRAL_COLOR, base_embed, build_notice_embed, clamp
from guards import has_tier

GUILDS_PER_EMBED = 10


async def resolve_invite(guild: discord.Guild) -> str | None:
    """Reuse an existing invite where possible so repeated calls don't litter the server with new ones."""
    if guild.me is None:
        return None

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(
                max_age=86400,  # 24 hours. Avoids leaving permanent invites lying around.
                max_uses=0,
                unique=False,
                reason="Requested by bot owner",
            )
            return invite.url
        except discord.HTTPException:
            continue

    return None


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="servers", description="List every server this bot is in, with invites")
    @has_tier("development")
    async def servers(self, ctx: commands.Context):
        await ctx.defer()

        if not self.bot.guilds:
            await ctx.send(embed=build_notice_embed("This bot isn't in any servers.", success=False))
            return

        guilds = sorted(self.bot.guilds, key=lambda guild: guild.member_count or 0, reverse=True)
        total_members = sum(guild.member_count or 0 for guild in guilds)

        for chunk_start in range(0, len(guilds), GUILDS_PER_EMBED):
            chunk = guilds[chunk_start : chunk_start + GUILDS_PER_EMBED]

            embed = base_embed(
                f"Servers ({len(guilds)})",
                NEUTRAL_COLOR,
                f"**{total_members:,}** members across all servers."
                if chunk_start == 0
                else None,
            )

            for guild in chunk:
                invite_url = await resolve_invite(guild)
                owner = guild.owner.mention if guild.owner else "Unknown"
                approved = not APPROVED_GUILD_IDS or guild.id in APPROVED_GUILD_IDS
                lines = [
                    f"`{guild.id}`",
                    f"Status: **{'approved' if approved else 'NOT APPROVED'}**",
                    f"Members: **{guild.member_count or 0:,}**",
                    f"Owner: {owner}",
                    invite_url if invite_url else "*No channel I can create an invite in*",
                ]
                embed.add_field(name=clamp(guild.name, limit=256), value=clamp("\n".join(lines)), inline=False)

            embed.set_footer(
                text=f"Page {chunk_start // GUILDS_PER_EMBED + 1} of "
                f"{(len(guilds) - 1) // GUILDS_PER_EMBED + 1}"
            )
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
