"""Small, atomic configuration file containing preferences only."""

import json
import os
import tempfile
from pathlib import Path

from platformdirs import user_config_path

from ynab_cli.errors import CliError


def config_path() -> Path:
    directory = os.environ.get("YNAB_CONFIG_DIR")
    return (Path(directory) if directory else user_config_path("cli-for-ynab")) / "config.json"


def read_config() -> dict:
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CliError("invalid_config", f"Cannot read configuration at {path}.", 2) from exc
    if not isinstance(data, dict):
        raise CliError("invalid_config", f"Configuration at {path} must be a JSON object.", 2)
    return data


def write_config(data: dict) -> None:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.write("\n")
        try:
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    except OSError as exc:
        raise CliError("config_write_failed", f"Cannot save configuration at {path}.") from exc


def resolve_plan(explicit: str | None) -> str:
    config = read_config()
    plan = explicit or os.environ.get("YNAB_BUDGET_ID") or config.get("default_budget")
    if not isinstance(plan, str) or not plan.strip():
        raise CliError(
            "budget_required", "Use --budget ID or run 'ynab budgets set-default ID'.", 2
        )
    aliases = config.get("aliases", {})
    if not isinstance(aliases, dict):
        raise CliError("invalid_config", "Budget aliases must be a JSON object.", 2)
    resolved = aliases.get(plan, plan)
    if not isinstance(resolved, str) or not resolved.strip():
        raise CliError("invalid_config", "Budget alias must refer to a budget ID.", 2)
    return resolved
