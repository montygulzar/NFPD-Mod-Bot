"""Alt account likelihood detector with invite tracking.

Posts a risk report to the server-log channel for EVERY member who joins.
Low-risk members get a brief summary; medium/high risk get a full breakdown.

Invite tracking: caches invite use counts so we can tell which invite was used
when someone joins (Discord does not expose this directly).

Requires the bot to have Manage Guild permission to fetch invite lists.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

import embeds as embeds_module
from config import BRAND_NAME
from modlog import post_to_server_log_channel

logger = logging.getLogger("modbot.alt_detector")

MAX_SCORE   = 100
HIGH_RISK   = 60
MEDIUM_RISK = 30

# Minimum gap between full invite re-caches of one guild, so repeated gateway
# reconnects don't turn into repeated invite fetches for every guild.
CACHE_REFRESH_SECONDS = 300.0

COLOR_HIGH   = 0xD93A3A
COLOR_MEDIUM = 0xF5A524
COLOR_LOW    = 0x3BA55D

# Username has 4+ digits in a row (common alt pattern like "user12345")
_DIGIT_RUN = re.compile(r"\d{4,}")


def _age_days(dt: datetime) -> int:
    return (datetime.now(timezone.utc) - dt).days


def _score_member(
    member: discord.Member,
    inviter: discord.Member | discord.User | None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    age = _age_days(member.created_at)
    if age < 1:
        score += 65
        reasons.append(f"Account created **today** ({age}d old)")
    elif age < 7:
        score += 45
        reasons.append(f"Very new account ({age}d old)")
    elif age < 30:
        score += 25
        reasons.append(f"Account under 30 days old ({age}d old)")
    elif age < 90:
        score += 10
        reasons.append(f"Account under 90 days old ({age}d old)")

    # member.avatar is None means no custom profile picture set
    if member.avatar is None:
        score += 20
        reasons.append("No custom profile picture (default avatar)")

    if _DIGIT_RUN.search(member.name):
        score += 15
        reasons.append(f"Username has a digit run (`{member.name}`)")

    if inviter is not None:
        inviter_age = _age_days(inviter.created_at)
        if inviter_age < 30:
            score += 20
            reasons.append(f"Invited by new account ({inviter_age}d old: {inviter})")
        elif inviter_age < 90:
            score += 5
            reasons.append(f"Invited by relatively new account ({inviter_age}d old: {inviter})")

    # The factors add up past 100 when several land at once (a day-old account with a
    # default avatar, a digit-run name and a new inviter scores 120), which the report
    # then renders as "120 / 100".
    return min(score, MAX_SCORE), reasons


def _risk_label(score: int) -> tuple[str, int]:
    if score >= HIGH_RISK:
        return "HIGH", COLOR_HIGH
    if score >= MEDIUM_RISK:
        return "MEDIUM", COLOR_MEDIUM
    return "LOW", COLOR_LOW


def _build_embed(
    member: discord.Member,
    score: int,
    reasons: list[str],
    invite_code: str | None,
    inviter: discord.Member | discord.User | None,
) -> discord.Embed:
    label, color = _risk_label(score)
    embed = discord.Embed(
        title=f"\U0001F916  Alt Score - {label}",
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Risk score", value=f"**{score}** / {MAX_SCORE}", inline=True)
    embed.add_field(
        name="Account age",
        value=f"{discord.utils.format_dt(member.created_at, 'R')}\n({_age_days(member.created_at)}d old)",
        inline=True,
    )
    if inviter:
        embed.add_field(name="Invited by", value=f"{inviter.mention}\n`{inviter.id}`", inline=True)
    if invite_code:
        embed.add_field(name="Invite code", value=f"`{invite_code}`", inline=True)
    if reasons:
        embed.add_field(
            name="Factors",
            value="\n".join(f"- {r}" for r in reasons) if reasons else "None",
            inline=False,
        )
    embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
    return embed


class AltDetector(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # {guild_id: {invite_code: (uses, inviter_id)}}
        self._cache: dict[int, dict[str, tuple[int, int | None]]] = {}
        # {guild_id: monotonic timestamp of last full invite refresh}
        self._cached_at: dict[int, float] = {}

    async def _cache_guild(self, guild: discord.Guild, *, force: bool = False) -> None:
        """Snapshot the current invite use counts for a guild.

        Throttled because on_ready fires on every gateway reconnect, and a brief
        network blip would otherwise trigger one invite fetch per guild each time.
        The cache still refreshes after a genuine outage, just not on every resume.
        """
        if guild.me is None:
            return

        now = time.monotonic()
        last = self._cached_at.get(guild.id)
        if not force and last is not None and now - last < CACHE_REFRESH_SECONDS:
            return
        self._cached_at[guild.id] = now

        if not guild.me.guild_permissions.manage_guild:
            logger.warning(
                "Alt detector cannot cache invites for %s (%s) - missing Manage Guild permission",
                guild.name, guild.id,
            )
            return
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {
                inv.code: (inv.uses or 0, inv.inviter.id if inv.inviter else None)
                for inv in invites
            }
            logger.debug("Cached %d invites for %s", len(invites), guild.name)
        except discord.HTTPException as error:
            logger.warning("Could not fetch invites for %s (%s): %s", guild.name, guild.id, error)

    async def _find_used_invite(
        self, guild: discord.Guild
    ) -> tuple[str | None, discord.Member | discord.User | None]:
        """Compare cached vs current invite uses to find which one was just used."""
        if guild.me is None or not guild.me.guild_permissions.manage_guild:
            return None, None

        old = self._cache.get(guild.id, {})
        try:
            current = await guild.invites()
        except discord.HTTPException:
            return None, None

        # Update cache immediately so the next join has fresh baseline
        self._cache[guild.id] = {
            inv.code: (inv.uses or 0, inv.inviter.id if inv.inviter else None)
            for inv in current
        }

        for inv in current:
            old_uses, inviter_id = old.get(inv.code, (0, None))
            if (inv.uses or 0) > old_uses:
                inviter_id = inv.inviter.id if inv.inviter else None
                return inv.code, await self._resolve_user(guild, inviter_id)

        # Invite may have been deleted (reached max_uses)
        for code, (_, inviter_id) in old.items():
            if not any(inv.code == code for inv in current):
                return code, await self._resolve_user(guild, inviter_id)

        return None, None

    async def _resolve_user(
        self, guild: discord.Guild, user_id: int | None
    ) -> discord.Member | discord.User | None:
        if user_id is None:
            return None
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._cache_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._cache_guild(guild, force=True)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self._cache.setdefault(invite.guild.id, {})[invite.code] = (
            invite.uses or 0,
            invite.inviter.id if invite.inviter else None,
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self._cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        invite_code, inviter = await self._find_used_invite(member.guild)
        score, reasons = _score_member(member, inviter)
        embed = _build_embed(member, score, reasons, invite_code, inviter)
        await post_to_server_log_channel(member.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AltDetector(bot))
