"""APPEAL_URL: the Appeals server link in ban DMs comes from .env, not code."""
import pytest

from tests.test_config import _load_config

BASE = {"BOT_TOKEN": "test-token", "DATABASE_URL": "postgresql://u:p@localhost:5432/db"}


@pytest.fixture(autouse=True)
def restore_config():
    yield
    _load_config(**BASE, APPEAL_URL=None)


def test_unset_keeps_existing_invite():
    cfg = _load_config(**BASE, APPEAL_URL=None)
    assert cfg.APPEAL_URL == cfg.DEFAULT_APPEAL_URL == "https://discord.gg/xnKvzXw5Vd"


def test_blank_keeps_existing_invite():
    cfg = _load_config(**BASE, APPEAL_URL="   ")
    assert cfg.APPEAL_URL == cfg.DEFAULT_APPEAL_URL


def test_custom_url_used_in_ban_dm_and_button():
    _load_config(**BASE, APPEAL_URL="https://discord.gg/newinvite")
    import embeds
    from views import BanAppealView

    embed = embeds.build_ban_dm_embed("Breaking rules")
    appeals_field = next(f for f in embed.fields if f.name == "Appeals")
    assert "https://discord.gg/newinvite" in appeals_field.value

    button = BanAppealView().children[0]
    assert button.url == "https://discord.gg/newinvite"


@pytest.mark.parametrize("bad", ["discord.gg/abc", "not a url", "https://discord.gg/a b"])
def test_malformed_url_stops_startup(bad):
    # A malformed link would make Discord reject the whole ban DM, so it is
    # reported with the other config errors instead of failing silently later.
    with pytest.raises(SystemExit) as exc:
        _load_config(**BASE, APPEAL_URL=bad)
    assert exc.value.code == 2
