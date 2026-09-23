"""Tests for configuration parsing and validation."""
import os
import importlib


def _load_config(**env_overrides):
    """Reload config module with specific environment variables."""
    original = {}
    for key in list(env_overrides) + [
        "BOT_TOKEN", "DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD",
        "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_PORT", "OWNER_IDS",
        "GLOBAL_ACTION_ROLE_IDS", "APPROVED_GUILD_IDS",
        "GLOBAL_ACTION_EXEMPT_GUILD_IDS", "LEAVE_UNAPPROVED_GUILDS",
        "PROTECTED_USER_IDS", "BLOCKED_USER_IDS", "COMMAND_PREFIX",
    ]:
        original[key] = os.environ.get(key)
        if key not in env_overrides:
            os.environ.pop(key, None)

    for key, value in env_overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    try:
        import config
        return importlib.reload(config)
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestConfigValidation:
    def test_missing_bot_token_exits(self):
        try:
            _load_config(
                DATABASE_URL="postgresql://u:p@localhost:5432/db",
            )
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2

    def test_missing_database_url_exits(self):
        try:
            _load_config(BOT_TOKEN="test-token")
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2

    def test_valid_config_loads(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
        )
        assert cfg.BOT_TOKEN == "test-token"
        assert cfg.DATABASE_URL == "postgresql://u:p@localhost:5432/db"

    def test_postgres_vars_assemble_url(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            POSTGRES_USER="myuser",
            POSTGRES_PASSWORD="mypass",
            POSTGRES_HOST="dbhost",
            POSTGRES_DB="mydb",
            POSTGRES_PORT="5433",
        )
        assert cfg.DATABASE_URL == "postgresql://myuser:mypass@dbhost:5433/mydb"

    def test_postgres_password_percent_encoded(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            POSTGRES_USER="user",
            POSTGRES_PASSWORD="p@ss/w:rd#1",
            POSTGRES_HOST="host",
            POSTGRES_DB="db",
        )
        assert "@" not in cfg.DATABASE_URL.split("@")[0].split(":")[-1]
        assert "p%40ss%2Fw%3Ard%231" in cfg.DATABASE_URL

    def test_database_url_not_double_encoded(self):
        url = "postgresql://user:p%40ss@host:5432/db"
        cfg = _load_config(BOT_TOKEN="test-token", DATABASE_URL=url)
        assert cfg.DATABASE_URL == url

    def test_invalid_database_scheme_exits(self):
        try:
            _load_config(
                BOT_TOKEN="test-token",
                DATABASE_URL="mysql://u:p@localhost/db",
            )
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2

    def test_pool_max_less_than_min_exits(self):
        try:
            _load_config(
                BOT_TOKEN="test-token",
                DATABASE_URL="postgresql://u:p@localhost:5432/db",
                DB_POOL_MIN_SIZE="10",
                DB_POOL_MAX_SIZE="5",
            )
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2

    def test_redacted_database_url_hides_credentials(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            DATABASE_URL="postgresql://secretuser:secretpass@myhost:5432/mydb",
        )
        redacted = cfg.redacted_database_url()
        assert "secretuser" not in redacted
        assert "secretpass" not in redacted
        assert "myhost" in redacted
        assert "mydb" in redacted
        assert "***" in redacted

    def test_global_action_exempt_guild_ids_parsed(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
            GLOBAL_ACTION_EXEMPT_GUILD_IDS="111,222,333",
        )
        assert cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS == {111, 222, 333}

    def test_empty_exempt_guild_ids_means_none_exempt(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
            GLOBAL_ACTION_EXEMPT_GUILD_IDS="",
        )
        assert cfg.GLOBAL_ACTION_EXEMPT_GUILD_IDS == set()

    def test_invalid_exempt_guild_ids_exits(self):
        try:
            _load_config(
                BOT_TOKEN="test-token",
                DATABASE_URL="postgresql://u:p@localhost:5432/db",
                GLOBAL_ACTION_EXEMPT_GUILD_IDS="abc,def",
            )
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2

    def test_id_list_single_value(self):
        cfg = _load_config(
            BOT_TOKEN="test-token",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
            OWNER_IDS="12345",
        )
        assert cfg.OWNER_IDS == {12345}

    def test_empty_command_prefix_exits(self):
        try:
            _load_config(
                BOT_TOKEN="test-token",
                DATABASE_URL="postgresql://u:p@localhost:5432/db",
                COMMAND_PREFIX="",
            )
            assert False, "Should have raised SystemExit"
        except SystemExit as e:
            assert e.code == 2
