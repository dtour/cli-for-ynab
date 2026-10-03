import calendar
import re
from datetime import date

from ynab_cli.errors import CliError


def parse_date(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CliError("invalid_date", "Use a valid date in YYYY-MM-DD format.", 2) from exc


def parse_month(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise CliError("invalid_month", "Use a month in YYYY-MM format.", 2)
    return parse_date(value + "-01")


def month_range(value: str) -> tuple[date, date]:
    first = parse_month(value)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])
