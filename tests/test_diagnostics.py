"""Tests for diagnostics and config validation."""
from unittest.mock import patch

import pytest

import diagnostics


class TestValidateConfig:
    def test_warns_exempt_not_in_approved(self):
        with patch("diagnostics.config") as cfg:
            cfg.OWNER_IDS = {1}
            cfg.APPROVED_GUILD_IDS = {100, 200}
            cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS = {300}
            cfg.LEAVE_UNAPPROVED_GUILDS = False
            cfg.COMMAND_PREFIX = "!"
            cfg.PROTECTED_USER_IDS = set()
            cfg.BLOCKED_USER_IDS = set()
            cfg.MOD_ROLE_IDS = {10}
            cfg.BAN_PERM_ROLE_IDS = set()
            cfg.CR_ROLE_IDS = {10}
            cfg.MANAGEMENT_ROLE_IDS = set()
            cfg.OWNERSHIP_ROLE_IDS = set()
            cfg.DEVELOPMENT_ROLE_IDS = set()

            warnings = diagnostics.validate_config()
            assert any("GLOBAL_ACTION_EXEMPT_GUILD_IDS" in w for w in warnings)

    def test_no_warning_when_exempt_subset_of_approved(self):
        with patch("diagnostics.config") as cfg:
            cfg.OWNER_IDS = {1}
            cfg.APPROVED_GUILD_IDS = {100, 200, 300}
            cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS = {200}
            cfg.LEAVE_UNAPPROVED_GUILDS = False
            cfg.COMMAND_PREFIX = "!"
            cfg.PROTECTED_USER_IDS = set()
            cfg.BLOCKED_USER_IDS = set()
            cfg.MOD_ROLE_IDS = {10}
            cfg.BAN_PERM_ROLE_IDS = set()
            cfg.CR_ROLE_IDS = {10}
            cfg.MANAGEMENT_ROLE_IDS = set()
            cfg.OWNERSHIP_ROLE_IDS = set()
            cfg.DEVELOPMENT_ROLE_IDS = set()

            warnings = diagnostics.validate_config()
            assert not any("GLOBAL_ACTION_EXEMPT_GUILD_IDS" in w for w in warnings)

    def test_warns_no_owner_ids(self):
        with patch("diagnostics.config") as cfg:
            cfg.OWNER_IDS = set()
            cfg.APPROVED_GUILD_IDS = set()
            cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS = set()
            cfg.LEAVE_UNAPPROVED_GUILDS = False
            cfg.COMMAND_PREFIX = "!"
            cfg.PROTECTED_USER_IDS = set()
            cfg.BLOCKED_USER_IDS = set()
            cfg.MOD_ROLE_IDS = set()
            cfg.BAN_PERM_ROLE_IDS = set()
            cfg.CR_ROLE_IDS = set()
            cfg.MANAGEMENT_ROLE_IDS = set()
            cfg.OWNERSHIP_ROLE_IDS = set()
            cfg.DEVELOPMENT_ROLE_IDS = set()

            warnings = diagnostics.validate_config()
            assert any("OWNER_IDS" in w for w in warnings)

    def test_warns_leave_unapproved_without_approved(self):
        with patch("diagnostics.config") as cfg:
            cfg.OWNER_IDS = {1}
            cfg.APPROVED_GUILD_IDS = set()
            cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS = set()
            cfg.LEAVE_UNAPPROVED_GUILDS = True
            cfg.COMMAND_PREFIX = "!"
            cfg.PROTECTED_USER_IDS = set()
            cfg.BLOCKED_USER_IDS = set()
            cfg.MOD_ROLE_IDS = {10}
            cfg.BAN_PERM_ROLE_IDS = set()
            cfg.CR_ROLE_IDS = {10}
            cfg.MANAGEMENT_ROLE_IDS = set()
            cfg.OWNERSHIP_ROLE_IDS = set()
            cfg.DEVELOPMENT_ROLE_IDS = set()

            warnings = diagnostics.validate_config()
            assert any("LEAVE_UNAPPROVED_GUILDS" in w for w in warnings)


class TestCommandStats:
    def test_records_and_retrieves(self):
        diagnostics._invocations.clear()
        diagnostics._errors.clear()

        diagnostics.record_invocation("kick")
        diagnostics.record_invocation("kick")
        diagnostics.record_invocation("ban")
        diagnostics.record_error("ban")

        top, total_inv, total_err = diagnostics.get_command_stats()
        assert total_inv == 3
        assert total_err == 1
        assert top[0] == ("kick", 2)
