"""Tier-aware /help: an embedded menu that only lists the commands you can run."""
import discord
from discord import app_commands
from discord.ext import commands

from config import BRAND_NAME
from embeds import NEUTRAL_COLOR, base_embed, build_notice_embed, clamp
from guards import (
    _TIERS,
    is_blocked,
    is_protected,
    member_tier_index,
    special_grants,
    tier_granting_role_ids,
    tier_index,
    tier_label,
)

CATEGORIES = {
    "Moderation":        ("Moderation",       "\U0001F528", "Warn, mute, kick and ban"),
    "CaseManagement":    ("Cases",            "\U0001F4C1", "Moderation history and stats"),
    "ChannelModeration": ("Channels",         "\U0001F512", "Purge, slowmode and lockdowns"),
    "GlobalModeration":  ("Global",           "\U0001F310", "Actions across every NFPD server"),
    "Settings":          ("Settings",         "⚙️", "Log channels, raid and warn config"),
    "AltDetector":       ("Alt Detection",    "\U0001F916", "Alt account alerts and config"),
    "Backup":            ("Backups",          "\U0001F4BE", "Snapshot and restore roles/channels"),
    "Owner":             ("Developer",        "\U0001F6E0️", "Bot management"),
    "Debug":             ("Diagnostics",      "\U0001FA7A", "Health and debug reports"),
}


def command_tier(command: commands.Command) -> str | None:
    for check in command.checks:
        tier = getattr(check, "nfpd_tier", None)
        if tier is not None:
            return tier
    return None


def visible_commands(bot: commands.Bot, user_tier: int | None) -> dict[str, list[tuple[commands.Command, str]]]:
    grouped: dict[str, list[tuple[commands.Command, str]]] = {}
    if user_tier is None:
        return grouped
    for command in bot.walk_commands():
        if isinstance(command, commands.Group):
            continue
        tier = command_tier(command)
        if tier is None or tier_index(tier) > user_tier or command.cog_name not in CATEGORIES:
            continue
        grouped.setdefault(command.cog_name, []).append((command, tier))
    return {cog: sorted(items, key=lambda item: item[0].qualified_name) for cog, items in grouped.items()}


def command_mention(bot: commands.Bot, command: commands.Command) -> str:
    ids = getattr(bot, "app_command_ids", {})
    root = command.qualified_name.split(" ")[0]
    if root in ids:
        return f"</{command.qualified_name}:{ids[root]}>"
    return f"`/{command.qualified_name}`"


def tier_ladder(user_tier: int | None) -> str:
    steps = []
    for index, tier in enumerate(_TIERS):
        label = tier_label(index)
        steps.append(f"**__{label}__**" if index == user_tier else label)
    return " → ".join(steps)


def build_home_embed(bot: commands.Bot, user: discord.abc.User, user_tier: int | None, grouped) -> discord.Embed:
    embed = base_embed(f"\U0001F4D6  {BRAND_NAME} Help", NEUTRAL_COLOR)
    if user_tier is None:
        embed.description = (
            f"Hey {user.mention}! You don't hold a staff tier, so there are no moderation "
            "commands for you here."
        )
    else:
        total = sum(len(items) for items in grouped.values())
        embed.description = (
            f"Hey {user.mention}! You're **{tier_label(user_tier)}**, with **{total}** commands available.\n"
            "Pick a category from the menu below."
        )
        for cog_name, (label, emoji, blurb) in CATEGORIES.items():
            if cog_name in grouped:
                embed.add_field(
                    name=f"{emoji}  {label}",
                    value=f"{blurb}\n`{len(grouped[cog_name])} command(s)`",
                    inline=True,
                )
    embed.add_field(name="Tiers", value=tier_ladder(user_tier), inline=False)
    return embed


def build_permissions_embed(
    bot: commands.Bot,
    target: discord.abc.User,
    target_tier: int | None,
    grouped,
) -> discord.Embed:
    """Third-person view of what another member is allowed to run."""
    embed = base_embed(f"\U0001F50D  Permissions  •  {target}", NEUTRAL_COLOR)
    embed.set_thumbnail(url=target.display_avatar.url)

    total = sum(len(items) for items in grouped.values())
    if target_tier is None:
        embed.description = (
            f"{target.mention} holds **no staff tier**, so no moderation commands are "
            "available to them."
        )
    else:
        embed.description = (
            f"{target.mention} is **{tier_label(target_tier)}**, with **{total}** "
            "command(s) available.\nPick a category below to see exactly which ones."
        )

    granting = tier_granting_role_ids(target)
    if granting:
        embed.add_field(
            name="Tier roles",
            value=clamp(", ".join(f"<@&{role_id}>" for role_id in granting)),
            inline=False,
        )

    grants = special_grants(target)
    if grants:
        embed.add_field(name="Direct grants", value="\n".join(grants), inline=False)

    if is_blocked(target.id):
        embed.add_field(
            name="⛔  Blocked",
            value="This user is blocked from using the bot entirely.",
            inline=False,
        )
    if is_protected(target.id):
        embed.add_field(
            name="\U0001F6E1  Protected",
            value="This user can't be the target of a moderation action.",
            inline=False,
        )

    for cog_name, (label, emoji, blurb) in CATEGORIES.items():
        if cog_name in grouped:
            embed.add_field(
                name=f"{emoji}  {label}",
                value=f"{blurb}\n`{len(grouped[cog_name])} command(s)`",
                inline=True,
            )

    embed.add_field(name="Tiers", value=tier_ladder(target_tier), inline=False)
    return embed


def build_category_embed(bot: commands.Bot, cog_name: str, items) -> discord.Embed:
    label, emoji, blurb = CATEGORIES[cog_name]
    lines = []
    for command, tier in items:
        badge = tier_label(tier_index(tier))
        lines.append(f"{command_mention(bot, command)}  •  `{badge}+`\n{command.description or command.short_doc or ''}")
    embed = base_embed(
        f"{emoji}  {label}",
        NEUTRAL_COLOR,
        f"*{blurb}*\n\n" + "\n\n".join(lines),
    )
    return embed


class HelpView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        author: discord.abc.User,
        user_tier: int | None,
        grouped,
        subject: discord.abc.User | None = None,
    ):
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author.id
        self.author = author
        self.user_tier = user_tier
        self.grouped = grouped
        # None means the menu describes the author's own access; otherwise it
        # describes `subject`, and Home renders the third-person view.
        self.subject = subject
        self.message: discord.Message | None = None

        options = [discord.SelectOption(label="Home", value="__home__", emoji="\U0001F3E0", description="Back to the overview")]
        for cog_name, (label, emoji, blurb) in CATEGORIES.items():
            if cog_name in grouped:
                options.append(
                    discord.SelectOption(
                        label=label, value=cog_name, emoji=emoji,
                        description=f"{blurb} ({len(grouped[cog_name])})"[:100],
                    )
                )
        self.category.options = options
        if len(options) == 1:
            self.category.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=build_notice_embed("Run `/help` yourself to get your own menu.", success=False),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.category.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(placeholder="Choose a category...")
    async def category(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        if choice == "__home__":
            if self.subject is not None:
                embed = build_permissions_embed(self.bot, self.subject, self.user_tier, self.grouped)
            else:
                embed = build_home_embed(self.bot, self.author, self.user_tier, self.grouped)
        else:
            embed = build_category_embed(self.bot, choice, self.grouped[choice])
        await interaction.response.edit_message(embed=embed, view=self)


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Show the commands you can use")
    @app_commands.describe(member="Optionally look up another member's permissions (staff only)")
    async def help(self, ctx: commands.Context, member: discord.Member | None = None):
        # No member given (or you pointed it at yourself) keeps the original
        # first-person menu, including in DMs where the converter never runs.
        if member is None or member.id == ctx.author.id:
            user_tier = member_tier_index(ctx.author)
            grouped = visible_commands(self.bot, user_tier)
            view = HelpView(self.bot, ctx.author, user_tier, grouped)
            view.message = await ctx.send(
                embed=build_home_embed(self.bot, ctx.author, user_tier, grouped), view=view, ephemeral=True
            )
            return

        if member_tier_index(ctx.author) is None:
            await ctx.send(
                embed=build_notice_embed(
                    "Only staff can look up another member's permissions.", success=False
                ),
                ephemeral=True,
            )
            return

        target_tier = member_tier_index(member)
        grouped = visible_commands(self.bot, target_tier)
        view = HelpView(self.bot, ctx.author, target_tier, grouped, subject=member)
        view.message = await ctx.send(
            embed=build_permissions_embed(self.bot, member, target_tier, grouped), view=view, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
