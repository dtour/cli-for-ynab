import pytest
from conftest import BUDGET, OTHER_BUDGET
from keyring.errors import KeyringError

from ynab_cli import auth
from ynab_cli.config import config_path, read_config, resolve_plan, write_config
from ynab_cli.errors import CliError


def test_environment_explicitly_overrides_keychain(monkeypatch):
    auth.store_token("keychain-token")
    monkeypatch.setenv("YNAB_API_TOKEN", "environment-token")
    assert auth.credential() == ("environment-token", "environment")


def test_logout_removes_stored_credential():
    auth.keyring.set_password(auth.LEGACY_SERVICE, auth.ACCOUNT, "legacy-token")
    auth.store_token("secret-token")
    auth.delete_token()
    with pytest.raises(CliError) as error:
        auth.credential()
    assert error.value.code == "not_authenticated"


def test_existing_login_survives_project_rename():
    auth.keyring.set_password(auth.LEGACY_SERVICE, auth.ACCOUNT, "legacy-token")
    assert auth.credential() == ("legacy-token", "keychain")


def test_new_login_takes_precedence_over_legacy_login():
    auth.keyring.set_password(auth.LEGACY_SERVICE, auth.ACCOUNT, "legacy-token")
    auth.store_token("new-token")
    assert auth.credential() == ("new-token", "keychain")


def test_keychain_failure_has_no_plaintext_fallback(monkeypatch):
    def fail(*args):
        raise KeyringError("backend diagnostics")

    monkeypatch.setattr(auth.keyring, "set_password", fail)
    with pytest.raises(CliError) as error:
        auth.store_token("secret-token")
    assert "secret-token" not in str(error.value)
    assert not config_path().exists()


def test_budget_precedence_and_aliases(monkeypatch):
    write_config({"aliases": {"main": BUDGET, "other": OTHER_BUDGET}, "default_budget": BUDGET})
    assert resolve_plan(None) == BUDGET
    monkeypatch.setenv("YNAB_BUDGET_ID", "other")
    assert resolve_plan(None) == OTHER_BUDGET
    assert resolve_plan("main") == BUDGET
    assert read_config()["aliases"]["other"] == OTHER_BUDGET


def test_config_corruption_is_not_silently_reset():
    write_config({"default_budget": BUDGET})
    config_path().write_text("{broken")
    with pytest.raises(CliError) as error:
        read_config()
    assert error.value.code == "invalid_config"
