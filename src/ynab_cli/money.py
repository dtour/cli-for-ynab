"""Exact conversion at the CLI boundary; the API and JSON output use milliunits."""

import re
from decimal import Decimal

from ynab_cli.errors import CliError

MAX_MILLIUNITS = 2**53 - 1


def to_milliunits(value: str) -> int:
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,3})?", value) or len(value) > 24:
        raise CliError(
            "invalid_amount", "Use a decimal amount with at most three decimal places.", 2
        )
    result = int(Decimal(value) * 1000)
    if abs(result) > MAX_MILLIUNITS:
        raise CliError("invalid_amount", "Amount exceeds the supported range.", 2)
    return result


def from_milliunits(value: int) -> str:
    return format(Decimal(value) / 1000, ".3f")
