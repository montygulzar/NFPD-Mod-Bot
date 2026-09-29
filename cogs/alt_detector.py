"""Alt account likelihood detector with invite tracking.

Posts a risk report to the alt-log channel (or server-log / mod-log fallback)
for EVERY member who joins. Low-risk joins get a compact summary; medium and
high risk get a full breakdown with the factors that contributed to the score.

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
from discord import app_commands
from discord.ext import commands

import embeds as embeds_module
from config import BRAND_NAME
from database import get_guild_settings, set_alt_log_channel
from guards import has_tier
from modlog import post_to_alt_log_channel

logger = logging.getLogger("modbot.alt_detector")

MAX_SCORE   = 100
HIGH_RISK   = 60
MEDIUM_RISK = 30

CACHE_REFRESH_SECONDS = 300.0

COLOR_HIGH   = 0xD93A3A
COLOR_MEDIUM = 0xF5A524
COLOR_LOW    = 0x3BA55D

_DIGIT_RUN = re.compile(r"\d{4,}")


def _age_days(dt: datetime) -> int:
    return (datetime.now(timezone.utc) - dt).days


def _score_member(
    member: discord.Member,
    inviter: discord.Member | discord.User | None,
) -> tuple[int, list[tuple[int, str]]]:
    """Return (clamped score, list of (points, reason)) pairs."""
    factors: list[tuple[int, str]] = []

    age = _age_days(member.created_at)
    if age < 1:
        factors.append((40, f"Account created **today** ({age}d old)"))
    elif age < 7:
        factors.append((30, f"Very new account (**{age}d** old)"))
    elif age < 30:
        factors.append((20, f"Account under 30 days old (**{age}d**)"))
    elif age < 90:
        factors.append((10, f"Account under 90 days old (**{age}d**)"))

    if member.avatar is None:
        factors.append((15, "No custom profile picture"))

    if _DIGIT_RUN.search(member.name):
        factors.append((10, f"Username has a digit run (`{member.name}`)"))

    if not member.avatar and not member.global_name:
        factors.append((10, "No global display name set"))

    if inviter is not None:
        inviter_age = _age_days(inviter.created_at)
        if inviter_age < 30:
            factors.append((15, f"Invited by new account (**{inviter_age}d** old: {inviter})"))
        elif inviter_age < 90:
            factors.append((5, f"Invited by relatively new account (**{inviter_age}d** old: {inviter})"))

    raw = sum(pts for pts, _ in factors)
    return min(raw, MAX_SCORE), factors


def _risk_label(score: int) -> tuple[str, str, int]:
    """Return (label, emoji, color)."""
    if score >= HIGH_RISK:
        return "HIGH RISK", "\U0001F6A8", COLOR_HIGH
    if score >= MEDIUM_RISK:
        return "MEDIUM RISK", "⚠️", COLOR_MEDIUM
    return "LOW RISK", "✅", COLOR_LOW


def _score_bar(score: int) -> str:
    filled = round(score / MAX_SCORE * 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty + f"  **{score}**/{MAX_SCORE}"


def _build_embed(
    member: discord.Member,
    score: int,
    factors: list[tuple[int, str]],
    invite_code: str | None,
    inviter: discord.Member | discord.User | None,
) -> discord.Embed:
    label, emoji, color = _risk_label(score)
    embed = discord.Embed(color=color, timestamp=discord.utils.utcnow())
    embed.set_author(
        name=f"{emoji}  Alt Detection  •  {label}",
        icon_url=embeds_module.BRAND_ICON_URL,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="User",
        value=f"{member.mention}\n`{member.id}`",
        inline=True,
    )
    embed.add_field(
        name="Account Created",
        value=f"{discord.utils.format_dt(member.created_at, 'R')}\n({_age_days(member.created_at)}d old)",
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)

    embed.add_field(
        name="Risk Score",
        value=_score_bar(score),
        inline=False,
    )

    if inviter:
        invite_info = f"Invited by {inviter.mention} (`{inviter.id}`)"
        if invite_code:
            invite_info += f"\nCode: `{invite_code}`"
        embed.add_field(name="Invite", value=invite_info, inline=False)
    elif invite_code:
        embed.add_field(name="Invite Code", value=f"`{invite_code}`", inline=True)

    if factors:
        lines = []
        for pts, reason in factors:
            lines.append(f"`+{pts:>2}` {reason}")
        embed.add_field(
            name=f"Factors ({len(factors)})",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(name="Factors", value="*No risk factors detected*", inline=False)

    embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
    return embed


class AltDetector(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, dict[str, tuple[int, int | None]]] = {}
        self._cached_at: dict[int, float] = {}

    async def _cache_guild(self, guild: discord.Guild, *, force: bool = False) -> None:
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
        if guild.me is None or not guild.me.guild_permissions.manage_guild:
            return None, None

        old = self._cache.get(guild.id, {})
        try:
            current = await guild.invites()
        except discord.HTTPException:
            return None, None

        self._cache[guild.id] = {
            inv.code: (inv.uses or 0, inv.inviter.id if inv.inviter else None)
            for inv in current
        }

        for inv in current:
            old_uses, _ = old.get(inv.code, (0, None))
            if (inv.uses or 0) > old_uses:
                inviter_id = inv.inviter.id if inv.inviter else None
                return inv.code, await self._resolve_user(guild, inviter_id)

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
        score, factors = _score_member(member, inviter)
        embed = _build_embed(member, score, factors, invite_code, inviter)
        await post_to_alt_log_channel(member.guild, embed)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="setaltalertchannel",
        description="Set where alt-detection alerts are posted",
    )
    @app_commands.describe(channel="Channel for alt alerts, or leave blank to use the server-log channel")
    @commands.guild_only()
    @has_tier("ownership")
    async def setaltalertchannel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ):
        from embeds import build_notice_embed

        await set_alt_log_channel(ctx.guild.id, channel.id if channel else None)
        if channel is None:
            await ctx.send(embed=build_notice_embed(
                "Alt-alert channel cleared. Alerts will fall back to the server-log or mod-log channel."
            ))
        else:
            await ctx.send(embed=build_notice_embed(f"Alt-detection alerts will now post to {channel.mention}."))


async def setup(bot: commands.Bot):
    await bot.add_cog(AltDetector(bot))
