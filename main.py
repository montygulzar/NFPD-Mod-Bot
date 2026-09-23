"""Entry point.

Startup order is deliberate:

    1. configure logging, so every later failure is reported the same way
    2. start the health server, so /health answers probes immediately and /ready
       reports 503 while dependencies come up
    3. wait for Postgres (retrying, not crashing) - commands are useless without it
    4. connect to Discord

Shutdown is driven by SIGTERM, which is what `docker stop` sends. Python's default
SIGTERM handling kills the process outright without unwinding, so the pool would
never be closed; an explicit handler turns it into an orderly shutdown instead.
"""
import asyncio
import logging
import signal

import discord
from discord.ext import commands

import config
import database
import diagnostics
import guards
from config import BOT_TOKEN, COMMAND_PREFIX
from embeds import build_notice_embed, set_brand_icon
from health import HealthServer
from logging_setup import configure_logging

logger = logging.getLogger("modbot")

INITIAL_COGS = (
    "cogs.moderation",
    "cogs.case_management",
    "cogs.global_moderation",
    "cogs.settings",
    "cogs.channel_moderation",
    "cogs.raid_protection",
    "cogs.scheduled_tasks",
    "cogs.owner",
    "cogs.guild_guard",
    "cogs.debug",
    "cogs.server_logs",
    "cogs.alt_detector",
    "cogs.backup",
)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.members = True          # privileged: member join/leave/update events
    intents.message_content = True  # privileged: message content in edit/delete logs
    intents.invites = True          # on_invite_create / on_invite_delete
    return intents


class ModBot(commands.Bot):
    """The bot, with extension loading and command sync moved into setup_hook.

    setup_hook runs exactly once per process, after login but before the gateway
    connects. on_ready fires again on every reconnect, so anything that must happen
    only once belongs here.
    """

    def __init__(self) -> None:
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=build_intents(),
            help_command=None,
            # Member chunking is left ON deliberately. The global commands resolve
            # targets with guild.get_member(), which returns None for an uncached
            # member - disabling the startup chunk would make globalkick/globalmute
            # silently skip guilds instead of acting in them.
            #
            # Bot messages may never ping @everyone or a role. Reasons are supplied
            # by moderators and end up in message content, so this is enforced at the
            # client rather than trusted to each call site.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.health = HealthServer(self)

    async def setup_hook(self) -> None:
        loaded, failed = 0, []
        for extension in INITIAL_COGS:
            try:
                await self.load_extension(extension)
                loaded += 1
            except Exception:
                # One broken cog must not stop the rest of the bot from running.
                failed.append(extension)
                logger.exception("Failed to load extension %s", extension)
        logger.info("Loaded %d/%d extensions", loaded, len(INITIAL_COGS))
        if failed:
            logger.error("Extensions unavailable this run: %s", ", ".join(failed))

        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash command(s)", len(synced))
        except discord.HTTPException as error:
            # Usually a rate limit. The bot still works via prefix commands and the
            # previously-registered slash commands, so this is not fatal.
            logger.warning("Slash command sync failed (%s) - existing commands remain registered", error)

    async def on_ready(self) -> None:
        self.health.note_connected()
        if self.user is not None:
            set_brand_icon(self.user.display_avatar.url)
            logger.info(
                "Connected as %s (%s) across %d guild(s)",
                self.user, self.user.id, len(self.guilds),
            )
        for warning in diagnostics.validate_config():
            logger.warning("Config check: %s", warning)

    async def on_resumed(self) -> None:
        self.health.note_connected()
        logger.info("Gateway session resumed")

    async def on_disconnect(self) -> None:
        # Fires on every brief gateway drop; discord.py reconnects on its own. Logged
        # at debug because a healthy bot does this routinely.
        self.health.note_disconnected()
        logger.debug("Disconnected from the gateway")

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        # Default discord.py behaviour prints to stderr, bypassing our formatter.
        logger.exception("Unhandled exception in event %s", event_method)


def unwrap_error(error: BaseException) -> BaseException:
    """Slash invocations arrive wrapped in HybridCommandError/CommandInvokeError. Peel to the real cause."""
    seen = {id(error)}
    while (original := getattr(error, "original", None)) is not None and id(original) not in seen:
        seen.add(id(original))
        error = original
    return error


def describe_error(error: BaseException) -> tuple[str, bool]:
    """Map an exception to (user-facing message, should_log)."""
    if isinstance(error, commands.MissingPermissions):
        return "You don't have permission to use that.", False
    if isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(permission.replace("_", " ") for permission in error.missing_permissions)
        return f"I'm missing the required permission(s): {missing}.", False
    if isinstance(error, commands.NoPrivateMessage):
        return "That command only works inside a server.", False
    if isinstance(error, commands.PrivateMessageOnly):
        return "That command only works in a DM to the bot.", False
    if isinstance(error, guards.BlockedUser):
        return "You are not permitted to use this bot.", False
    if isinstance(error, database.DatabaseUnavailable):
        return (
            "I can't reach the database right now, so that command was not applied. "
            "It should recover on its own - try again shortly.",
            True,
        )
    if isinstance(error, commands.CheckFailure):
        return str(error) if str(error) else "You don't have permission to use that.", False
    if isinstance(error, discord.Forbidden):
        return "Discord refused that action. Check my permissions and that my role sits above the target's.", False
    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing argument: `{error.param.name}`.", False
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        return "I couldn't find that user.", False
    if isinstance(error, commands.BadArgument):
        return "One of those arguments wasn't valid.", False
    return (
        f"Something went wrong running that command (`{type(error).__name__}`). Check the logs.",
        True,
    )


def register_handlers(bot: ModBot) -> None:
    @bot.check
    async def reject_blocked_users(ctx: commands.Context) -> bool:
        """Applies to every command in every cog, so a new command can't forget the deny list."""
        if guards.is_blocked(ctx.author.id):
            raise guards.BlockedUser("You are not permitted to use this bot.")
        return True

    @bot.event
    async def on_command_completion(ctx: commands.Context):
        if ctx.command is not None:
            diagnostics.record_invocation(ctx.command.qualified_name)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError):
        """Turn the common permission and usage failures into readable replies instead of tracebacks."""
        if isinstance(error, commands.CommandNotFound):
            return

        command_name = ctx.command.qualified_name if ctx.command else "unknown"
        if ctx.command is not None:
            diagnostics.record_error(command_name)

        error = unwrap_error(error)
        message, should_log = describe_error(error)

        if should_log:
            logger.exception(
                "Command %s failed for user %s in guild %s",
                command_name,
                ctx.author.id,
                ctx.guild.id if ctx.guild else "dm",
                exc_info=error,
            )

        try:
            await ctx.send(embed=build_notice_embed(message, success=False))
        except discord.HTTPException:
            logger.warning("Could not deliver the error reply for %s", command_name)


def install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Turn SIGTERM/SIGINT into a normal shutdown instead of an abrupt exit."""
    loop = asyncio.get_running_loop()

    def request_shutdown(signal_name: str) -> None:
        if shutdown.is_set():
            logger.warning("Received %s again - shutdown already in progress", signal_name)
            return
        logger.info("Received %s, shutting down", signal_name)
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig.name)
        except NotImplementedError:
            # Not supported on every platform; the KeyboardInterrupt path still works.
            logger.debug("Signal handler for %s unavailable on this platform", sig.name)


async def run() -> int:
    """Own the full lifecycle. Returns the process exit code."""
    shutdown = asyncio.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "Starting NFPD moderation bot (version=%s commit=%s)",
        config.APP_VERSION, config.GIT_COMMIT,
    )

    bot = ModBot()
    register_handlers(bot)
    exit_code = 0

    try:
        # Start the health server first so /health (liveness) answers probes while
        # the bot waits for Postgres. /ready correctly returns 503 until both the
        # database and Discord are up.
        await bot.health.start()

        # Wait for Postgres before touching Discord: connecting first would expose
        # commands that cannot record anything.
        try:
            await database.connect_database()
        except Exception:
            logger.critical("Database unavailable - cannot start", exc_info=True)
            return 1

        client = asyncio.create_task(bot.start(BOT_TOKEN), name="discord-client")
        signalled = asyncio.create_task(shutdown.wait(), name="shutdown-signal")
        done, _ = await asyncio.wait({client, signalled}, return_when=asyncio.FIRST_COMPLETED)

        if client in done:
            # The client stopped on its own - surface why.
            await cancel_task(signalled)
            try:
                client.result()
                logger.warning("Discord client stopped unexpectedly without an error")
                exit_code = 1
            except discord.LoginFailure:
                logger.critical("Discord rejected BOT_TOKEN. Correct it in .env and recreate the container.")
                exit_code = 1
            except discord.PrivilegedIntentsRequired:
                logger.critical(
                    "Privileged intents are not enabled for this application. Enable the "
                    "Server Members and Message Content intents in the Discord developer "
                    "portal, then restart."
                )
                exit_code = 1
            except Exception:
                logger.critical("Discord client failed", exc_info=True)
                exit_code = 1
        else:
            # Shutdown was signalled. Stop the client task before closing anything
            # it is using, and await it so the loop doesn't warn about a pending
            # task being destroyed.
            await cancel_task(client)
    finally:
        await shutdown_cleanly(bot)

    return exit_code


async def cancel_task(task: asyncio.Task) -> None:
    """Cancel a task and wait for it to finish unwinding."""
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Task %s raised while being cancelled", task.get_name(), exc_info=True)


async def shutdown_cleanly(bot: ModBot) -> None:
    """Release everything in reverse order of acquisition, never raising."""
    try:
        await bot.health.stop()
    except Exception:
        logger.exception("Error stopping the health server")

    try:
        if not bot.is_closed():
            # Closes the gateway, cancels cog task loops via cog_unload, and logs out.
            await asyncio.wait_for(bot.close(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("Discord client did not close within 15s")
    except Exception:
        logger.exception("Error closing the Discord client")

    try:
        await database.close_database()
    except Exception:
        logger.exception("Error closing the database pool")

    logger.info("Shutdown complete")


def main() -> int:
    configure_logging()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
