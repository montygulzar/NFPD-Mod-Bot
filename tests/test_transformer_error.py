"""TransformerError is raised whenever a discord.Member parameter resolves to
someone who is not in the guild. It must answer plainly instead of falling
through to the catch-all, which logs a full traceback on every occurrence."""
import discord
import pytest
from discord import app_commands
from discord.ext import commands

import main


def _transformer_error() -> app_commands.TransformerError:
    class _T(app_commands.Transformer):
        pass

    return app_commands.TransformerError(
        object(), discord.AppCommandOptionType.user, _T()
    )


def test_transformer_error_is_not_logged_as_a_crash():
    message, should_log = main.describe_error(_transformer_error())
    assert not should_log, "TransformerError must not log a traceback"
    assert "TransformerError" not in message, "raw exception type must not leak to the user"
    assert "left or been banned" in message


def test_transformer_error_survives_hybrid_wrapping():
    """Slash invocations arrive wrapped, so unwrap_error must peel to the real cause."""
    wrapped = commands.HybridCommandError(_transformer_error())
    unwrapped = main.unwrap_error(wrapped)
    assert isinstance(unwrapped, app_commands.TransformerError)

    message, should_log = main.describe_error(unwrapped)
    assert not should_log
    assert "left or been banned" in message


def test_unknown_errors_still_log():
    """The catch-all must keep logging genuine faults."""
    message, should_log = main.describe_error(RuntimeError("boom"))
    assert should_log
