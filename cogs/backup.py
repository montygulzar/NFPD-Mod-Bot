"""Server backup and restore.

/backupserver  - Snapshots all roles, channels, and permissions into a JSON file
                 and attaches it to the channel. Requires Ownership tier.
/restorebackup - Reads the most recent backup file and recreates any roles or
                 channels that no longer exist. Requires Ownership tier.

Backups are intentionally JSON files (not a database table) so they can be kept
offline and applied to a fresh server without needing database access.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import embeds as embeds_module
from config import BRAND_NAME
from embeds import DANGER_COLOR, NEUTRAL_COLOR, SUCCESS_COLOR, base_embed, build_notice_embed, clamp
from guards import has_tier
from views import ConfirmView


def _perms_to_dict(perms: discord.Permissions) -> dict[str, bool]:
    return {name: value for name, value in perms}


def _overwrites_to_list(
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite]
) -> list[dict]:
    result = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "type": "role" if isinstance(target, discord.Role) else "member",
            "id": target.id,
            "name": str(target),
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


def _snapshot_guild(guild: discord.Guild) -> dict:
    roles = []
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue
        roles.append({
            "id": role.id,
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "position": role.position,
            "permissions": _perms_to_dict(role.permissions),
        })

    channels = []
    for channel in sorted(guild.channels, key=lambda c: (c.position, c.name)):
        entry: dict = {
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": channel.category_id,
            "category_name": channel.category.name if channel.category else None,
            "overwrites": _overwrites_to_list(channel.overwrites),
        }
        if isinstance(channel, discord.TextChannel):
            entry["topic"] = channel.topic
            entry["nsfw"] = channel.nsfw
            entry["slowmode_delay"] = channel.slowmode_delay
        if isinstance(channel, discord.VoiceChannel):
            entry["bitrate"] = channel.bitrate
            entry["user_limit"] = channel.user_limit
        channels.append(entry)

    return {
        "meta": {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "member_count": guild.member_count,
        },
        "roles": roles,
        "channels": channels,
        "guild": {
            "verification_level": str(guild.verification_level),
            "default_notifications": str(guild.default_notifications),
        },
    }


async def _restore_roles(
    guild: discord.Guild,
    snapshot_roles: list[dict],
    reason: str,
) -> tuple[list[str], list[str]]:
    """Recreate any roles from the snapshot that no longer exist. Returns (created, failed)."""
    existing_ids = {r.id for r in guild.roles}
    created, failed = [], []

    for role_data in sorted(snapshot_roles, key=lambda r: r["position"]):
        if role_data["id"] in existing_ids:
            continue
        try:
            perms = discord.Permissions(**{
                k: v for k, v in role_data["permissions"].items()
                if hasattr(discord.Permissions, k)
            })
            await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=perms,
                reason=reason,
            )
            created.append(role_data["name"])
        except discord.HTTPException as error:
            failed.append(f"{role_data['name']}: {error}")

    return created, failed


async def _restore_channels(
    guild: discord.Guild,
    snapshot_channels: list[dict],
    reason: str,
) -> tuple[list[str], list[str]]:
    """Recreate any channels from the snapshot that no longer exist. Returns (created, failed)."""
    existing_ids = {c.id for c in guild.channels}
    created, failed = [], []

    # Categories first so text/voice channels can be placed inside them. Track the objects
    # we create as we go: guild.channels only lists them once the CHANNEL_CREATE gateway
    # event arrives, which can land after the child channels below have already been made.
    categories_by_name = {
        channel.name: channel
        for channel in guild.channels
        if isinstance(channel, discord.CategoryChannel)
    }

    for channel_data in snapshot_channels:
        if channel_data["id"] in existing_ids:
            continue
        if channel_data["type"] != "category":
            continue
        try:
            category = await guild.create_category(name=channel_data["name"], reason=reason)
            categories_by_name[category.name] = category
            created.append(f"#{channel_data['name']} (category)")
        except discord.HTTPException as error:
            failed.append(f"{channel_data['name']}: {error}")

    for channel_data in snapshot_channels:
        if channel_data["id"] in existing_ids:
            continue
        if channel_data["type"] == "category":
            continue

        category = categories_by_name.get(channel_data.get("category_name") or "")
        try:
            if channel_data["type"] == "text":
                await guild.create_text_channel(
                    name=channel_data["name"],
                    category=category,
                    topic=channel_data.get("topic"),
                    nsfw=channel_data.get("nsfw", False),
                    slowmode_delay=channel_data.get("slowmode_delay", 0),
                    reason=reason,
                )
            elif channel_data["type"] in ("voice", "stage_voice"):
                await guild.create_voice_channel(
                    name=channel_data["name"],
                    category=category,
                    bitrate=min(channel_data.get("bitrate", 64000), guild.bitrate_limit),
                    user_limit=channel_data.get("user_limit", 0),
                    reason=reason,
                )
            else:
                continue
            created.append(f"#{channel_data['name']}")
        except discord.HTTPException as error:
            failed.append(f"#{channel_data['name']}: {error}")

    return created, failed


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="backupserver", description="Snapshot all server roles and channels to a JSON file")
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_guild=True)
    async def backupserver(self, ctx: commands.Context) -> None:
        await ctx.defer()

        snapshot = _snapshot_guild(ctx.guild)
        taken_at = snapshot["meta"]["taken_at"][:10]  # YYYY-MM-DD

        file_bytes = json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8")
        file_size_kb = len(file_bytes) / 1024
        attachment = discord.File(
            io.BytesIO(file_bytes),
            filename=f"{ctx.guild.name}-backup-{taken_at}.json",
        )

        embed = base_embed(
            "\U0001F4BE  Server Backup Created",
            NEUTRAL_COLOR,
            f"Snapshot of **{ctx.guild.name}** taken {discord.utils.format_dt(discord.utils.utcnow(), 'f')}.",
        )
        embed.add_field(name="Roles", value=str(len(snapshot["roles"])), inline=True)
        embed.add_field(name="Channels", value=str(len(snapshot["channels"])), inline=True)
        embed.add_field(name="File size", value=f"{file_size_kb:.1f} KB", inline=True)
        embed.add_field(
            name="To restore",
            value="Use `/restorebackup` and attach this JSON file. Missing roles and channels will be recreated.",
            inline=False,
        )
        embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)

        await ctx.send(embed=embed, file=attachment)

    @commands.hybrid_command(
        name="restorebackup",
        description="Recreate missing roles and channels from a backup JSON file",
    )
    @app_commands.describe(backup_file="The .json backup file created by /backupserver")
    @commands.guild_only()
    @has_tier("ownership")
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def restorebackup(self, ctx: commands.Context, backup_file: discord.Attachment) -> None:
        if not backup_file.filename.endswith(".json"):
            await ctx.send(embed=build_notice_embed("Please attach a .json backup file.", success=False))
            return

        if backup_file.size > 5_000_000:
            await ctx.send(embed=build_notice_embed("Backup file is too large (max 5 MB).", success=False))
            return

        try:
            raw = await backup_file.read()
            snapshot = json.loads(raw)
            assert "roles" in snapshot and "channels" in snapshot
        except Exception:
            await ctx.send(embed=build_notice_embed("Could not parse the backup file. Make sure it was created by /backupserver.", success=False))
            return

        meta = snapshot.get("meta", {})
        original_guild = meta.get("guild_name", "unknown")
        taken_at = meta.get("taken_at", "unknown")[:10]

        view = ConfirmView(author_id=ctx.author.id)
        prompt = discord.Embed(
            title="Confirm Restore",
            description=(
                f"This will recreate any **missing** roles and channels from the `{original_guild}` "
                f"backup taken on **{taken_at}**.\n\n"
                "Existing roles and channels are **not** deleted or modified."
            ),
            color=DANGER_COLOR,
        )
        view.message = await ctx.send(embed=prompt, view=view)
        await view.wait()

        if not view.confirmed:
            await ctx.send(embed=build_notice_embed("Restore cancelled.", success=False))
            return

        # The interaction was already responded to by the confirm prompt above;
        # a second defer here would raise InteractionResponded on slash usage.
        reason = f"Server restore by {ctx.author} ({ctx.author.id}) from {original_guild} backup {taken_at}"

        roles_created, roles_failed = await _restore_roles(ctx.guild, snapshot["roles"], reason)
        channels_created, channels_failed = await _restore_channels(ctx.guild, snapshot["channels"], reason)

        embed = base_embed(
            "\U0001F4BE  Server Restore Complete",
            SUCCESS_COLOR if not roles_failed and not channels_failed else NEUTRAL_COLOR,
        )
        # These lists scale with the size of the server, so they have to be clamped:
        # a single over-length field makes Discord reject the whole embed and the
        # moderator would see nothing at all after a restore that did run.
        embed.add_field(
            name=f"Roles created ({len(roles_created)})",
            value=clamp(", ".join(f"`{r}`" for r in roles_created), empty="*None needed*"),
            inline=False,
        )
        embed.add_field(
            name=f"Channels created ({len(channels_created)})",
            value=clamp(", ".join(channels_created), empty="*None needed*"),
            inline=False,
        )
        if roles_failed or channels_failed:
            embed.add_field(
                name="Failures",
                value=clamp("\n".join(roles_failed + channels_failed)),
                inline=False,
            )
        embed.set_footer(text=BRAND_NAME, icon_url=embeds_module.BRAND_ICON_URL)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))
