import discord

import embeds as embeds_module
from config import BRAND_NAME
from embeds import DANGER_COLOR, NEUTRAL_COLOR, appeal_url, build_case_line


class BanAppealView(discord.ui.View):
    """A persistent link button attached to ban DMs so the recipient can appeal easily."""

    def __init__(self) -> None:
        # timeout=None means the button stays active indefinitely in the DM.
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Appeal your ban",
                style=discord.ButtonStyle.link,
                url=appeal_url(),
                emoji="📝",
            )
        )


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can respond to this.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(view=self)


class CasesPaginatorView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        member: discord.abc.User,
        case_rows: list,
        guild: discord.Guild,
        per_page: int = 5,
    ):
        super().__init__(timeout=120.0)
        self.author_id = author_id
        self.member = member
        self.case_rows = case_rows
        self.guild = guild
        self.per_page = per_page
        self.page = 0
        self.last_page = max(0, (len(case_rows) - 1) // per_page)
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.last_page

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        page_rows = self.case_rows[start : start + self.per_page]

        embed = discord.Embed(color=NEUTRAL_COLOR, timestamp=discord.utils.utcnow())
        # Read BRAND_ICON_URL through the module at call time, not at import time.
        # Importing it by value would bind to None permanently since set_brand_icon
        # updates the module attribute after views.py has already been imported.
        embed.set_author(
            name=f"Case History  \u2022  {self.member}",
            icon_url=embeds_module.BRAND_ICON_URL,
        )
        for row in page_rows:
            field_name, field_value = build_case_line(row, self.guild)
            embed.add_field(name=field_name, value=field_value, inline=False)

        embed.set_footer(
            text=f"Page {self.page + 1} of {self.last_page + 1}  \u2022  "
            f"{len(self.case_rows)} total  \u2022  {BRAND_NAME}",
            icon_url=embeds_module.BRAND_ICON_URL,
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can page through this.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.previous_button.disabled = True
        self.next_button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


def build_confirm_prompt(description: str) -> discord.Embed:
    embed = discord.Embed(
        title="Confirm Global Action",
        description=description,
        color=DANGER_COLOR,
    )
    embed.set_footer(
        text="This affects every server the bot is in.",
        icon_url=embeds_module.BRAND_ICON_URL,
    )
    return embed
