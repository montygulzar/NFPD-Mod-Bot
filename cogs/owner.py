import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import APPROVED_GUILD_IDS
from embeds import NEUTRAL_COLOR, audit_reason, base_embed, build_notice_embed, clamp
from guards import has_tier

logger = logging.getLogger("modbot.owner")

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

    @commands.hybrid_command(name="addrole", description="Add a role to a member")
    @app_commands.describe(member="The member to give the role to", role="The role to add", reason="Why")
    @commands.guild_only()
    @has_tier("development")
    @commands.bot_has_permissions(manage_roles=True)
    async def addrole(
        self,
        ctx: commands.Context,
        member: discord.Member,
        role: discord.Role,
        *,
        reason: str = "No reason provided",
    ):
        if role >= ctx.guild.me.top_role:
            await ctx.send(embed=build_notice_embed("That role is at or above my highest role.", success=False))
            return
        if role in member.roles:
            await ctx.send(embed=build_notice_embed(f"{member.mention} already has that role.", success=False))
            return

        await member.add_roles(role, reason=audit_reason(ctx.author, "Add role", reason))
        await ctx.send(embed=build_notice_embed(f"Added **{role.name}** to {member.mention}."))

    # --- Self-service role recovery -------------------------------------------
    # Prefix-only (commands.command, never synced to the slash tree) so these stay
    # out of the command picker entirely, and DM-capable so they still work after
    # you've stripped your own roles in the target server.

    async def _resolve(
        self, ctx: commands.Context, guild_id: int, role_id: int
    ) -> tuple[discord.Guild | None, discord.Member | None, discord.Role | None, str | None]:
        """Shared validation. Returns (guild, member, role, problem)."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, None, None, f"I'm not in any server with ID `{guild_id}`."
        if guild.me is None or not guild.me.guild_permissions.manage_roles:
            return None, None, None, f"I don't have Manage Roles in **{guild.name}**."

        member = guild.get_member(ctx.author.id)
        if member is None:
            try:
                member = await guild.fetch_member(ctx.author.id)
            except discord.HTTPException:
                return None, None, None, f"You aren't a member of **{guild.name}**."

        role = guild.get_role(role_id)
        if role is None:
            return None, None, None, f"No role with ID `{role_id}` exists in **{guild.name}**."
        if role.managed:
            return None, None, None, f"**{role.name}** is managed by an integration and can't be assigned."
        if role >= guild.me.top_role:
            return None, None, None, (
                f"**{role.name}** is at or above my highest role in **{guild.name}**, so I can't assign it."
            )
        return guild, member, role, None

    @commands.command(name="bypass")
    @has_tier("development")
    async def bypass(self, ctx: commands.Context, guild_id: int, role_id: int):
        """Give yourself a role in any server the bot is in. Usable in DMs."""
        guild, member, role, problem = await self._resolve(ctx, guild_id, role_id)
        if problem:
            await ctx.send(embed=build_notice_embed(problem, success=False))
            return

        if role in member.roles:
            await ctx.send(
                embed=build_notice_embed(f"You already have **{role.name}** in **{guild.name}**.", success=False)
            )
            return

        try:
            await member.add_roles(role, reason=audit_reason(ctx.author, "Bypass", "Self-restore"))
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Couldn't add that role: `{error}`", success=False))
            return

        # Self-granting a role is a privilege action, so it leaves a trail here as
        # well as in the server's own audit log.
        logger.warning(
            "BYPASS: %s (%s) self-granted role %s (%s) in %s (%s)",
            ctx.author, ctx.author.id, role.name, role.id, guild.name, guild.id,
        )
        await ctx.send(embed=build_notice_embed(f"Added **{role.name}** to you in **{guild.name}**."))

    @commands.command(name="unbypass")
    @has_tier("development")
    async def unbypass(self, ctx: commands.Context, guild_id: int, role_id: int):
        """Remove a role from yourself in any server the bot is in. Usable in DMs."""
        guild, member, role, problem = await self._resolve(ctx, guild_id, role_id)
        if problem:
            await ctx.send(embed=build_notice_embed(problem, success=False))
            return

        if role not in member.roles:
            await ctx.send(
                embed=build_notice_embed(f"You don't have **{role.name}** in **{guild.name}**.", success=False)
            )
            return

        try:
            await member.remove_roles(role, reason=audit_reason(ctx.author, "Bypass", "Self-removal"))
        except discord.HTTPException as error:
            await ctx.send(embed=build_notice_embed(f"Couldn't remove that role: `{error}`", success=False))
            return

        logger.warning(
            "BYPASS: %s (%s) self-removed role %s (%s) in %s (%s)",
            ctx.author, ctx.author.id, role.name, role.id, guild.name, guild.id,
        )
        await ctx.send(embed=build_notice_embed(f"Removed **{role.name}** from you in **{guild.name}**."))

    @commands.command(name="roles")
    @has_tier("development")
    async def roles(self, ctx: commands.Context, guild_id: int):
        """List assignable role IDs in a server, to find one for !bypass. Usable in DMs."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.send(embed=build_notice_embed(f"I'm not in any server with ID `{guild_id}`.", success=False))
            return

        member = guild.get_member(ctx.author.id)
        held = {role.id for role in member.roles} if member else set()
        top = guild.me.top_role if guild.me else None

        lines = []
        for role in sorted(guild.roles, reverse=True):
            if role.is_default():
                continue
            if role.managed or (top is not None and role >= top):
                marker = "⛔"  # can't be assigned by the bot
            elif role.id in held:
                marker = "✅"  # you already have it
            else:
                marker = " "
            lines.append(f"{marker} `{role.id}`  {role.name}")

        if not lines:
            await ctx.send(embed=build_notice_embed(f"**{guild.name}** has no roles beyond @everyone."))
            return

        # Role lists overflow a single embed description on larger servers.
        chunk: list[str] = []
        length = 0
        pages: list[list[str]] = []
        for line in lines:
            if length + len(line) + 1 > 3800:
                pages.append(chunk)
                chunk, length = [], 0
            chunk.append(line)
            length += len(line) + 1
        pages.append(chunk)

        for index, page in enumerate(pages, start=1):
            embed = base_embed(
                f"Roles  •  {clamp(guild.name, limit=200)}",
                NEUTRAL_COLOR,
                "\n".join(page),
            )
            embed.set_footer(text=f"Page {index}/{len(pages)}  •  ✅ you have it  ⛔ I can't assign it")
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
