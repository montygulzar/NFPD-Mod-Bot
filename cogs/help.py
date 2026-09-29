"""Tier-aware /help: an embedded menu that only lists the commands you can run."""
import discord
from discord.ext import commands

from config import BRAND_NAME
from embeds import NEUTRAL_COLOR, base_embed, build_notice_embed
from guards import _TIERS, member_tier_index, tier_index, tier_label

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
    def __init__(self, bot: commands.Bot, author: discord.abc.User, user_tier: int | None, grouped):
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author.id
        self.author = author
        self.user_tier = user_tier
        self.grouped = grouped
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
            embed = build_home_embed(self.bot, self.author, self.user_tier, self.grouped)
        else:
            embed = build_category_embed(self.bot, choice, self.grouped[choice])
        await interaction.response.edit_message(embed=embed, view=self)


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Show the commands you can use")
    async def help(self, ctx: commands.Context):
        user_tier = member_tier_index(ctx.author)
        grouped = visible_commands(self.bot, user_tier)
        view = HelpView(self.bot, ctx.author, user_tier, grouped)
        view.message = await ctx.send(
            embed=build_home_embed(self.bot, ctx.author, user_tier, grouped), view=view, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
