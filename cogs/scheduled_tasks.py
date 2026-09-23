import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from database import get_expired_temp_bans, remove_temp_ban
from modlog import record_case

logger = logging.getLogger("modbot.scheduled_tasks")


class ScheduledTasks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guilds already warned about missing Ban Members, so a permanently
        # misconfigured server doesn't emit an identical warning every minute.
        self._permission_warned: set[int] = set()
        self.expire_temp_bans.start()

    def cog_unload(self) -> None:
        self.expire_temp_bans.cancel()

    @tasks.loop(minutes=1)
    async def expire_temp_bans(self) -> None:
        # tasks.loop stops the loop permanently on an unhandled exception, so a single
        # transient database blip would silently end temp-ban expiry until the next
        # restart. Contain every failure to the tick it happened on.
        try:
            expired = await get_expired_temp_bans(datetime.now(timezone.utc))
        except Exception:
            logger.exception("Could not load expired temp-bans - retrying next tick")
            return

        for row in expired:
            try:
                await self._expire_one(row["guild_id"], row["user_id"])
            except Exception:
                logger.exception(
                    "Unhandled error expiring temp-ban for user %s in guild %s",
                    row["user_id"], row["guild_id"],
                )

    async def _expire_one(self, guild_id: int, user_id: int) -> None:
        """Lift one expired ban, clearing the record only once it can no longer be acted on.

        The record is deliberately kept when the unban fails for a fixable reason, so the
        ban is actually lifted on a later tick instead of the user staying banned forever.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # No longer in that server - nothing will ever act on this row.
            await remove_temp_ban(guild_id, user_id)
            return

        try:
            # Ban entries are keyed by ID, so this needs no user fetch and works
            # even for accounts that have since been deleted.
            await guild.unban(discord.Object(id=user_id), reason="Temporary ban expired")
        except discord.NotFound:
            await remove_temp_ban(guild_id, user_id)  # Already unbanned by hand.
            return
        except discord.Forbidden:
            if guild_id not in self._permission_warned:
                self._permission_warned.add(guild_id)
                logger.warning(
                    "Missing Ban Members in %s (%s) - temp-ban for user %s stays until it is restored",
                    guild.name, guild_id, user_id,
                )
            return
        except discord.HTTPException as error:
            logger.warning(
                "Could not lift temp-ban for user %s in %s (%s): %s",
                user_id, guild.name, guild_id, error,
            )
            return

        self._permission_warned.discard(guild_id)
        await remove_temp_ban(guild_id, user_id)

        if self.bot.user is None:
            return

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return  # Ban is lifted; only the case-log entry is lost.
        await record_case(guild, user, self.bot.user, "unban", "Temporary ban expired")

    @expire_temp_bans.before_loop
    async def before_expire_temp_bans(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduledTasks(bot))
