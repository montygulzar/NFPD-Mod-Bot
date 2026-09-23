import logging

import discord
from discord.ext import commands

from config import APPROVED_GUILD_IDS, LEAVE_UNAPPROVED_GUILDS, OWNER_IDS

logger = logging.getLogger("modbot.guild_guard")

UNAPPROVED_SERVER_MESSAGE = (
    "This server is not an approved NFPD server.\n"
    "If you believe this is a mistake, please DM the developer.\n\n"
    "**Developer:** omb\n"
    "**Username:** ombdeveloping\n"
    "**Discord ID:** `1285998518213017663`"
)


async def resolve_invite(guild: discord.Guild) -> str | None:
    """Try to generate a 24-hour invite from the first channel the bot can use."""
    if guild.me is None:
        return None
    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(
                max_age=86400,
                max_uses=1,
                unique=True,
                reason="Unapproved server alert - requested by bot owner",
            )
            return invite.url
        except discord.HTTPException:
            continue
    return None


async def post_in_server(guild: discord.Guild, message: str) -> None:
    """Post a message in the server's system channel, or the first writable text channel."""
    candidates = []
    if guild.system_channel is not None:
        candidates.append(guild.system_channel)
    candidates.extend(ch for ch in guild.text_channels if ch != guild.system_channel)

    if guild.me is None:
        return

    for channel in candidates:
        if not channel.permissions_for(guild.me).send_messages:
            continue
        try:
            await channel.send(message)
            return
        except discord.HTTPException:
            continue


async def get_inviter(guild: discord.Guild, bot_user_id: int) -> discord.User | None:
    """Look up who added the bot via the audit log.

    Catches HTTPException rather than just Forbidden: this runs on the join path, and
    letting any other API error escape would skip the in-server notice, the owner alert
    and the auto-leave that follow it.
    """
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            if entry.target and entry.target.id == bot_user_id:
                return entry.user
    except discord.HTTPException as error:
        logger.warning("Could not read the audit log in %s (%s): %s", guild.name, guild.id, error)
    return None


def build_unapproved_embed(
    guild: discord.Guild,
    inviter: discord.User | None,
    invite_url: str | None,
    *,
    leaving: bool,
) -> discord.Embed:
    embed = discord.Embed(
        title="Unapproved Server Join",
        color=0xDC2626,
        timestamp=discord.utils.utcnow(),
    )

    server_value = f"{guild.name}\n`{guild.id}`"
    embed.add_field(name="Server", value=server_value, inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)

    if inviter:
        embed.add_field(
            name="Added by",
            value=f"{inviter}\n`{inviter.id}`",
            inline=True,
        )
    else:
        embed.add_field(name="Added by", value="Unknown", inline=True)

    embed.add_field(
        name="Invite (24h)",
        value=invite_url if invite_url else "Could not generate invite",
        inline=False,
    )

    embed.set_footer(text="Leaving automatically." if leaving else "Auto-leave disabled - bot will remain.")
    return embed


class GuildGuard(commands.Cog):
    """Tracks which servers the bot has been added to and optionally refuses to stay in unapproved ones."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Unapproved guilds already reported. on_ready fires on every gateway
        # reconnect, and without this each one repeats the same warnings.
        self._reported: set[int] = set()

    def is_approved(self, guild: discord.Guild) -> bool:
        return not APPROVED_GUILD_IDS or guild.id in APPROVED_GUILD_IDS

    async def alert_owners(self, embed: discord.Embed) -> None:
        for owner_id in OWNER_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(embed=embed)
            except discord.HTTPException:
                continue

    async def _leave(self, guild: discord.Guild) -> None:
        try:
            await guild.leave()
            logger.warning("Left unapproved server %s (%s)", guild.name, guild.id)
        except discord.HTTPException as error:
            logger.warning(
                "Tried to leave unapproved server %s (%s) but failed: %s",
                guild.name, guild.id, error,
            )

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if self.is_approved(guild):
                self._reported.discard(guild.id)
                continue
            if guild.id not in self._reported:
                self._reported.add(guild.id)
                logger.warning("In unapproved server: %s (%s), owner %s", guild.name, guild.id, guild.owner_id)
            if LEAVE_UNAPPROVED_GUILDS:
                await self._leave(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        logger.info("Added to server: %s (%s), owner %s", guild.name, guild.id, guild.owner_id)

        if self.is_approved(guild):
            await self._alert_approved(guild)
            return

        # Fetch invite before potentially leaving - can't create one after the bot has left.
        invite_url = await resolve_invite(guild)
        inviter = await get_inviter(guild, self.bot.user.id)

        # Post notice in the server so the server owner knows why the bot left.
        await post_in_server(guild, UNAPPROVED_SERVER_MESSAGE)

        # DM all owners with the formatted embed.
        embed = build_unapproved_embed(
            guild, inviter, invite_url, leaving=LEAVE_UNAPPROVED_GUILDS
        )
        await self.alert_owners(embed)

        if LEAVE_UNAPPROVED_GUILDS:
            await self._leave(guild)

    async def _alert_approved(self, guild: discord.Guild) -> None:
        embed = discord.Embed(
            title="Joined Approved Server",
            color=0x16A34A,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`", inline=True)
        embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
        for owner_id in OWNER_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(embed=embed)
            except discord.HTTPException:
                continue


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildGuard(bot))
