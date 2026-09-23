import csv
import io

import discord
from discord import app_commands
from discord.ext import commands

import embeds as embeds_module
from database import (
    delete_case,
    get_action_counts,
    get_all_cases,
    get_case_by_id,
    get_cases_for_user,
    get_most_warned_users,
    get_top_moderators,
    update_case_reason,
)
from embeds import (
    AUDIT_REASON_LIMIT,
    MUTED_COLOR,
    NEUTRAL_COLOR,
    base_embed,
    build_notice_embed,
    clamp,
    format_timestamp,
    style_for,
)
from guards import has_tier
from modlog import post_to_log_channel
from views import CasesPaginatorView

CSV_COLUMNS = ("case_id", "user_id", "moderator_id", "action_type", "reason", "created_at")


def format_leaderboard(rows: list, guild: discord.Guild, id_column: str) -> str:
    lines = []
    for position, row in enumerate(rows, start=1):
        member = guild.get_member(row[id_column])
        name = member.mention if member else f"`{row[id_column]}`"
        lines.append(f"`{position}.` {name} - **{row['total']}**")
    return "\n".join(lines)


class CaseManagement(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="cases", description="View a member's moderation history")
    @app_commands.describe(member="The member to look up")
    @commands.guild_only()
    @has_tier("cr")
    async def cases(self, ctx: commands.Context, member: discord.Member):
        case_rows = await get_cases_for_user(ctx.guild.id, member.id)
        if not case_rows:
            await ctx.send(embed=build_notice_embed(f"{member.mention} has a clean record."))
            return

        view = CasesPaginatorView(ctx.author.id, member, case_rows, ctx.guild)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="casesearch", description="Look up a single case by its ID")
    @app_commands.describe(case_id="The case number to look up")
    @commands.guild_only()
    @has_tier("cr")
    async def casesearch(self, ctx: commands.Context, case_id: int):
        case_row = await get_case_by_id(ctx.guild.id, case_id)
        if case_row is None:
            await ctx.send(embed=build_notice_embed(f"No case #{case_id} in this server.", success=False))
            return

        target = self.bot.get_user(case_row["user_id"])
        if target is None:
            try:
                target = await self.bot.fetch_user(case_row["user_id"])
            except discord.NotFound:
                await ctx.send(
                    embed=build_notice_embed(
                        f"Case #{case_id} refers to a deleted account (`{case_row['user_id']}`).", success=False
                    )
                )
                return
        moderator = ctx.guild.get_member(case_row["moderator_id"])
        style = style_for(case_row["action_type"])

        embed = discord.Embed(color=style.color, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{style.icon}  Case #{case_row['id']}", icon_url=embeds_module.BRAND_ICON_URL)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.description = f"**{target}**\n`{target.id}`"
        embed.add_field(name="Action", value=style.title, inline=True)
        embed.add_field(
            name="Moderator",
            value=moderator.mention if moderator else f"`{case_row['moderator_id']}`",
            inline=True,
        )
        embed.add_field(name="Reason", value=clamp(case_row["reason"]), inline=False)
        embed.add_field(name="When", value=format_timestamp(case_row["created_at"]), inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="caseedit", description="Correct the reason on an existing case")
    @app_commands.describe(case_id="The case number to edit", new_reason="The corrected reason")
    @commands.guild_only()
    @has_tier("cr")
    async def caseedit(self, ctx: commands.Context, case_id: int, *, new_reason: str):
        if len(new_reason) > AUDIT_REASON_LIMIT:
            await ctx.send(
                embed=build_notice_embed(
                    f"Reason is too long ({len(new_reason)} chars, max {AUDIT_REASON_LIMIT}).", success=False
                )
            )
            return

        if not await update_case_reason(ctx.guild.id, case_id, new_reason):
            await ctx.send(embed=build_notice_embed(f"No case #{case_id} in this server.", success=False))
            return

        embed = base_embed(f"Case #{case_id} Edited", NEUTRAL_COLOR)
        embed.add_field(name="New reason", value=clamp(new_reason), inline=False)
        embed.add_field(name="Edited by", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)
        await post_to_log_channel(ctx.guild, embed)

    @commands.hybrid_command(name="casedelete", description="Permanently delete a case record")
    @app_commands.describe(case_id="The case number to delete")
    @commands.guild_only()
    @has_tier("cr")
    async def casedelete(self, ctx: commands.Context, case_id: int):
        if not await delete_case(ctx.guild.id, case_id):
            await ctx.send(embed=build_notice_embed(f"No case #{case_id} in this server.", success=False))
            return

        embed = base_embed(f"Case #{case_id} Deleted", MUTED_COLOR)
        embed.add_field(name="Deleted by", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)
        await post_to_log_channel(ctx.guild, embed)

    @commands.hybrid_command(name="caseexport", description="Export this server's full case history as a CSV file")
    @commands.guild_only()
    @has_tier("cr")
    async def caseexport(self, ctx: commands.Context):
        case_rows = await get_all_cases(ctx.guild.id)
        if not case_rows:
            await ctx.send(embed=build_notice_embed("No cases recorded in this server yet.", success=False))
            return

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for row in case_rows:
            writer.writerow(
                [row["id"], row["user_id"], row["moderator_id"], row["action_type"], row["reason"], row["created_at"]]
            )
        buffer.seek(0)

        export_file = discord.File(io.BytesIO(buffer.getvalue().encode()), filename=f"cases-{ctx.guild.id}.csv")
        embed = base_embed("Case Export", NEUTRAL_COLOR, f"{len(case_rows)} case(s) attached as CSV.")
        await ctx.send(embed=embed, file=export_file)

    @commands.hybrid_command(name="modstats", description="Moderation activity overview for this server")
    @commands.guild_only()
    @has_tier("cr")
    async def modstats(self, ctx: commands.Context):
        action_counts = await get_action_counts(ctx.guild.id)
        top_moderators = await get_top_moderators(ctx.guild.id)
        most_warned = await get_most_warned_users(ctx.guild.id)

        embed = discord.Embed(color=NEUTRAL_COLOR, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"Moderation Stats  \u2022  {ctx.guild.name}", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        if action_counts:
            total_cases = sum(row["total"] for row in action_counts)
            breakdown_lines = []
            for row in action_counts:
                style = style_for(row["action_type"])
                breakdown_lines.append(f"{style.icon}  {style.title} - **{row['total']}**")
            breakdown = "\n".join(breakdown_lines)
            embed.description = f"**{total_cases}** cases on record."
            embed.add_field(name="Breakdown", value=breakdown, inline=False)
        else:
            embed.description = "No cases recorded yet."

        if top_moderators:
            embed.add_field(
                name="Most Active Moderators",
                value=format_leaderboard(top_moderators, ctx.guild, "moderator_id"),
                inline=False,
            )
        if most_warned:
            embed.add_field(
                name="Most Warned Members",
                value=format_leaderboard(most_warned, ctx.guild, "user_id"),
                inline=False,
            )

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CaseManagement(bot))
