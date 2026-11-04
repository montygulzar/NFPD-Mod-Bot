"""Creating a throwaway invite to a server the bot is in.

Shared by /servers, which lists every server with a link to it, and by the
unapproved-server alert, which has to capture a link before the bot leaves.
Those two want different invites - one reusable, one single-use - so the
differences are parameters rather than a second copy of the walk.
"""
import discord

INVITE_MAX_AGE = 86400  # 24 hours, so nothing permanent is left lying around.


async def resolve_invite(
    guild: discord.Guild,
    *,
    max_uses: int = 0,
    unique: bool = False,
    reason: str = "Requested by bot owner",
) -> str | None:
    """An invite URL for *guild*, or None if no channel allows one.

    Tries each text channel in turn: the bot may lack Create Invite in some of
    them, and a channel can still refuse at the API level, so neither case is
    treated as fatal while another channel might work.

    With the defaults (unique=False, max_uses=0) Discord hands back an existing
    matching invite where it can, so repeated calls do not litter the server.
    """
    if guild.me is None:
        return None

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(
                max_age=INVITE_MAX_AGE,
                max_uses=max_uses,
                unique=unique,
                reason=reason,
            )
            return invite.url
        except discord.HTTPException:
            continue
    return None
