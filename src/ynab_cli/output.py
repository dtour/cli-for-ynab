import json
from dataclasses import dataclass

import typer


@dataclass(frozen=True)
class Output:
    pretty: bool = False

    def write(self, data: dict, *, budget: str | None = None, **metadata) -> None:
        meta = {"schema_version": 1, "money_unit": "milliunits", **metadata}
        if budget is not None:
            meta["budget_id"] = budget
        typer.echo(
            json.dumps(
                {"data": data, "meta": meta},
                indent=2 if self.pretty else None,
                separators=None if self.pretty else (",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
