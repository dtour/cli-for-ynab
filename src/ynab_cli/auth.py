"""Credentials stay in the OS keyring or an explicitly supplied environment variable."""

import os

import keyring
from keyring.errors import KeyringError

from ynab_cli.errors import CliError

SERVICE = "cli-for-ynab"
LEGACY_SERVICE = "ynab-cli-python"
ACCOUNT = "personal-access-token"


def validate_token(token: str) -> str:
    token = token.strip()
    if not token or any(char.isspace() for char in token):
        raise CliError("invalid_token", "Supply a nonempty token without whitespace.", 2)
    return token


def credential() -> tuple[str, str]:
    token = os.environ.get("YNAB_API_TOKEN")
    if token:
        return validate_token(token), "environment"
    try:
        token = keyring.get_password(SERVICE, ACCOUNT)
        if not token:
            token = keyring.get_password(LEGACY_SERVICE, ACCOUNT)
    except KeyringError as exc:
        raise CliError(
            "keychain_unavailable", "Cannot access Keychain. Unlock it or set YNAB_API_TOKEN."
        ) from exc
    if not token:
        raise CliError("not_authenticated", "Run 'ynab auth login' to store your token.", 3)
    return validate_token(token), "keychain"


def store_token(token: str) -> None:
    try:
        keyring.set_password(SERVICE, ACCOUNT, validate_token(token))
    except KeyringError as exc:
        raise CliError("keychain_unavailable", "Could not save the token in Keychain.") from exc


def delete_token() -> None:
    try:
        for service in (SERVICE, LEGACY_SERVICE):
            if keyring.get_password(service, ACCOUNT) is not None:
                keyring.delete_password(service, ACCOUNT)
    except KeyringError as exc:
        raise CliError("keychain_unavailable", "Could not remove the token from Keychain.") from exc
