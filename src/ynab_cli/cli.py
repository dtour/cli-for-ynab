"""Thin command definitions. Domain operations and SDK access live elsewhere."""

import getpass
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from typer import _click as click
from typer.core import TyperGroup

from ynab_cli import __version__, api_operations, auth, operations
from ynab_cli.api_registry import ENDPOINTS, endpoint
from ynab_cli.client import Client
from ynab_cli.config import read_config, resolve_plan, write_config
from ynab_cli.dates import month_range, parse_date, parse_month
from ynab_cli.errors import CliError
from ynab_cli.money import to_milliunits
from ynab_cli.output import Output
from ynab_cli.reports import spending


def emit_error(error: CliError) -> None:
    typer.echo(json.dumps(error.payload(), separators=(",", ":")), err=True)


class JsonGroup(TyperGroup):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CliError as exc:
            emit_error(exc)
            raise typer.Exit(exc.exit_code) from exc
        except click.ClickException as exc:
            emit_error(CliError("usage_error", exc.format_message(), 2))
            raise typer.Exit(2) from exc
        except ValidationError as exc:
            emit_error(CliError("invalid_input", "Input failed validation. Check command help.", 2))
            raise typer.Exit(2) from exc


app = typer.Typer(
    cls=JsonGroup,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="YNAB for people and agents. JSON output; use --budget before the command.",
)
auth_app = typer.Typer(help="Store and validate credentials.")
budgets_app = typer.Typer(help="List budgets and configure local aliases.")
accounts_app = typer.Typer(help="Inspect accounts.")
categories_app = typer.Typer(help="Inspect categories and preview allocations and targets.")
transactions_app = typer.Typer(help="Query and categorize transactions.")
reports_app = typer.Typer(help="Calculate spending from transaction data.")
changes_app = typer.Typer(help="Apply a reviewed allocation or target proposal.")
api_app = typer.Typer(help="Every SDK operation: discover schemas, read data, and preview writes.")
for name, group in [
    ("auth", auth_app),
    ("budgets", budgets_app),
    ("accounts", accounts_app),
    ("categories", categories_app),
    ("transactions", transactions_app),
    ("reports", reports_app),
    ("changes", changes_app),
    ("api", api_app),
]:
    app.add_typer(group, name=name)


@dataclass
class State:
    budget_hint: str | None
    output: Output
    _client: Client | None = field(default=None, repr=False)

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client()
        return self._client

    @property
    def budget(self) -> str:
        return resolve_plan(self.budget_hint)

    def close(self):
        if self._client:
            self._client.close()


def state(ctx: typer.Context) -> State:
    return ctx.find_root().obj


def version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    budget: Annotated[
        str | None, typer.Option("--budget", "-b", help="Budget UUID or local alias.")
    ] = None,
    pretty: Annotated[bool, typer.Option(help="Indent JSON output.")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=version_callback, is_eager=True)
    ] = False,
):
    ctx.obj = State(budget, Output(pretty))
    ctx.call_on_close(ctx.obj.close)


@auth_app.command("login")
def login(
    ctx: typer.Context,
    token_stdin: Annotated[bool, typer.Option(help="Read token from stdin.")] = False,
):
    if token_stdin:
        token = sys.stdin.read(4096)
    elif sys.stdin.isatty():
        token = getpass.getpass("YNAB personal access token: ", stream=sys.stderr)
    else:
        raise CliError("token_required", "Use an interactive terminal or --token-stdin.", 2)
    token = auth.validate_token(token)
    client = Client(token)
    try:
        user = client.user()
        auth.store_token(token)
    finally:
        client.close()
    state(ctx).output.write(
        {
            "authenticated": True,
            "user_id": user["id"],
            "stored_in": "keychain",
            "environment_override_active": bool(os.environ.get("YNAB_API_TOKEN")),
        }
    )


@app.command("guide")
def agent_guide():
    """Print the agent usage and approval policy."""
    resource = files("ynab_cli").joinpath("agent-guide.md")
    if resource.is_file():
        typer.echo(resource.read_text(encoding="utf-8"))
    else:
        typer.echo((Path(__file__).parents[2] / "docs" / "agents.md").read_text())


@auth_app.command("status")
def auth_status(ctx: typer.Context):
    _, source = auth.credential()
    user = state(ctx).client.user()
    state(ctx).output.write({"authenticated": True, "source": source, "user_id": user["id"]})


@auth_app.command("logout")
def logout(ctx: typer.Context):
    auth.delete_token()
    state(ctx).output.write(
        {
            "keychain_token_removed": True,
            "environment_override_active": bool(os.environ.get("YNAB_API_TOKEN")),
        }
    )


@budgets_app.command("list")
def budgets_list(ctx: typer.Context):
    state(ctx).output.write(
        {"budgets": state(ctx).client.plans(), "aliases": read_config().get("aliases", {})}
    )


@budgets_app.command("alias")
def budget_alias(ctx: typer.Context, name: str, budget_id: str):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
        raise CliError(
            "invalid_alias", "Use lowercase letters, digits, - or _, starting with a letter.", 2
        )
    plan = state(ctx).client.plan(budget_id)
    config = read_config()
    config.setdefault("aliases", {})[name] = plan["id"]
    write_config(config)
    state(ctx).output.write({"alias": name, "budget_id": plan["id"], "name": plan["name"]})


@budgets_app.command("set-default")
def budget_default(ctx: typer.Context, budget: str):
    plan = state(ctx).client.plan(resolve_plan(budget))
    config = read_config()
    config["default_budget"] = plan["id"]
    write_config(config)
    state(ctx).output.write({"default_budget": plan["id"], "name": plan["name"]})


@accounts_app.command("list")
def accounts_list(ctx: typer.Context):
    s = state(ctx)
    s.output.write({"accounts": s.client.accounts(s.budget)}, budget=s.budget)


@categories_app.command("list")
def categories_list(ctx: typer.Context):
    s = state(ctx)
    s.output.write({"category_groups": s.client.categories(s.budget)}, budget=s.budget)


@categories_app.command("view")
def category_view(ctx: typer.Context, category_id: str, month: str | None = None):
    s = state(ctx)
    selected_month = parse_month(month) if month else None
    s.output.write(
        {"category": s.client.category(s.budget, category_id, selected_month)}, budget=s.budget
    )


def write_proposal(s: State, ops: list[operations.Operation], save: Path | None):
    plan = operations.proposal(s.client, s.budget, ops)
    write_saved_proposal(s, plan, save)


def write_saved_proposal(s: State, plan, save: Path | None):
    if save:
        try:
            # Exclusive creation prevents silently replacing a previously reviewed proposal.
            descriptor = os.open(save, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(plan.model_dump(), handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            raise CliError(
                "proposal_write_failed",
                "Cannot create proposal file. Choose a new writable path.",
                2,
            ) from exc
    s.output.write(
        {"proposal": plan.model_dump()},
        budget=plan.budget_id,
        status="requires_approval",
        saved_to=str(save) if save else None,
    )


@categories_app.command("allocate")
def category_allocate(
    ctx: typer.Context,
    category_id: str,
    month: Annotated[str, typer.Option(help="Budget month, YYYY-MM.")],
    amount: Annotated[str, typer.Option(help="New total assigned amount in budget currency.")],
    save: Annotated[Path | None, typer.Option(help="Save proposal to a new JSON file.")] = None,
):
    s = state(ctx)
    op = operations.allocation(
        s.client, s.budget, category_id, parse_month(month), to_milliunits(amount)
    )
    write_proposal(s, [op], save)


@categories_app.command("move")
def category_move(
    ctx: typer.Context,
    source: Annotated[str, typer.Option("--from", help="Source category UUID.")],
    destination: Annotated[str, typer.Option("--to", help="Destination category UUID.")],
    month: Annotated[str, typer.Option(help="Budget month, YYYY-MM.")],
    amount: Annotated[str, typer.Option(help="Amount to move in budget currency.")],
    save: Path | None = None,
):
    s = state(ctx)
    ops = operations.move(
        s.client, s.budget, source, destination, parse_month(month), to_milliunits(amount)
    )
    write_proposal(s, ops, save)


class Frequency(StrEnum):
    monthly = "monthly"
    weekly = "weekly"
    yearly = "yearly"


class Rollover(StrEnum):
    refill = "refill"
    add = "add"


@categories_app.command("target")
def category_target(
    ctx: typer.Context,
    category_id: str,
    amount: Annotated[str, typer.Option(help="Target amount in budget currency.")],
    frequency: Frequency | None = None,
    due: Annotated[str | None, typer.Option(help="Target date, YYYY-MM-DD.")] = None,
    rollover: Rollover | None = None,
    save: Path | None = None,
):
    if frequency and due:
        raise CliError("invalid_target", "Choose --frequency or --due.", 2)
    changes = {"goal_target": to_milliunits(amount)}
    if changes["goal_target"] < 0:
        raise CliError("invalid_target", "A target amount must be nonnegative.", 2)
    if frequency:
        changes["goal_frequency"] = frequency.value
    if due:
        changes["goal_target_date"] = parse_date(due).isoformat()
    if rollover:
        changes["goal_needs_whole_amount"] = rollover == Rollover.add
    s = state(ctx)
    write_proposal(s, [operations.target(s.client, s.budget, category_id, changes)], save)


def read_json(path: str):
    try:
        if path == "-":
            text = sys.stdin.read(1_048_577)
        else:
            with Path(path).open(encoding="utf-8") as handle:
                text = handle.read(1_048_577)
        if len(text) > 1_048_576:
            raise CliError("input_too_large", "JSON input must be at most 1 MiB.", 2)
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise CliError(
            "invalid_json", "Cannot read a valid JSON document from the specified input.", 2
        ) from exc


@changes_app.command("apply")
def changes_apply(
    ctx: typer.Context,
    file: str,
    approve: Annotated[str, typer.Option(help="Exact ID of the proposal approved by the user.")],
):
    s = state(ctx)
    value = api_operations.unwrap(read_json(file))
    handler = (
        api_operations if isinstance(value, dict) and value.get("kind") == "api" else operations
    )
    plan = handler.load_proposal(value)
    if s.budget_hint and s.budget != plan.budget_id:
        raise CliError("budget_mismatch", "Selected budget differs from the reviewed proposal.", 2)
    s.output.write(handler.apply(s.client, plan, approve), budget=plan.budget_id)


def api_json(value: str):
    """JSON literal, @file, or stdin; file/stdin reads share the existing size limit."""
    if value.startswith("@") or value == "-":
        return read_json(value[1:] if value.startswith("@") else value)
    try:
        if len(value) > 1_048_576:
            raise CliError("input_too_large", "JSON input must be at most 1 MiB.", 2)
        return json.loads(value)
    except ValueError as exc:
        raise CliError(
            "invalid_json", "Use a JSON object, @file.json, or '-' for stdin.", 2
        ) from exc


@api_app.command("list")
def api_list(ctx: typer.Context, resource: str | None = None):
    """List all registered SDK operations without credentials."""
    rows = [spec.describe() for spec in ENDPOINTS.values()]
    if resource:
        rows = [row for row in rows if resource.casefold() in row["resource"].casefold()]
    state(ctx).output.write({"operations": rows, "count": len(rows)})


@api_app.command("schema")
def api_schema(ctx: typer.Context, operation: str):
    """Show parameter and request-body JSON schemas for an SDK operation."""
    state(ctx).output.write(endpoint(operation).describe(schema=True))


@api_app.command("call")
def api_call(
    ctx: typer.Context,
    operation: str,
    params: Annotated[
        str,
        typer.Option(help="Parameters as JSON, @file.json, or '-'. Budget comes from --budget."),
    ] = "{}",
    data: Annotated[
        str | None,
        typer.Option(
            help="SDK request body as JSON, @file.json, or '-'. Amounts are integer milliunits."
        ),
    ] = None,
    save: Annotated[
        Path | None, typer.Option(help="Save a write proposal for review and changes apply.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Validate reads without sending; preview routine categorization.")
    ] = False,
):
    """Call an SDK operation. General writes always produce approval proposals."""
    spec = endpoint(operation)
    if params in {"-", "@-"} and data in {"-", "@-"}:
        raise CliError("invalid_api_input", "Only one input can read stdin.", 2)
    values, body = api_operations.normalize(
        spec.name, api_json(params), api_json(data) if data is not None else None
    )
    s = state(ctx)
    budget = s.budget if spec.budget_scoped else None
    if not spec.writes:
        if save:
            raise CliError("invalid_api_input", "--save is for write proposals.", 2)
        if dry_run:
            s.output.write(
                {"operation": spec.name, "params": values}, budget=budget, status="validated"
            )
        else:
            s.output.write(
                s.client.invoke(spec.name, budget, values), budget=budget, operation=spec.name
            )
    elif not save and api_operations.routine_updates(spec.name, values, body):
        result = api_operations.categorize(
            s.client, spec.name, budget, values, body, dry_run=dry_run
        )
        s.output.write(result, budget=budget, operation=spec.name)
    else:
        plan = api_operations.preview(s.client, spec.name, budget, values, body)
        write_saved_proposal(s, plan, save)


class TransactionKind(StrEnum):
    uncategorized = "uncategorized"
    unapproved = "unapproved"


@transactions_app.command("list")
def transactions_list(
    ctx: typer.Context,
    since: str | None = None,
    until: str | None = None,
    kind: Annotated[TransactionKind | None, typer.Option("--type")] = None,
    account: str | None = None,
    category: str | None = None,
    payee: str | None = None,
    fields: str | None = None,
    limit: Annotated[int, typer.Option(min=0, help="Maximum rows; 0 returns all.")] = 100,
    knowledge: Annotated[
        int | None, typer.Option(min=0, help="Delta cursor from a previous response.")
    ] = None,
):
    start = parse_date(since) if since else date.today() - timedelta(days=365)
    end = parse_date(until) if until else date.today()
    if start > end:
        raise CliError("invalid_range", "--since must be on or before --until.", 2)
    if knowledge is not None and (not since or limit or account or category or payee or fields):
        raise CliError(
            "invalid_delta_query",
            "Delta queries require explicit --since, --limit 0, "
            "and no local filters or field selection.",
            2,
        )
    s = state(ctx)
    result = s.client.transactions(
        s.budget, since=start, until=end, kind=kind.value if kind else None, knowledge=knowledge
    )
    rows = result["transactions"]
    if account:
        rows = [row for row in rows if row.get("account_id") == account]
    if category:
        rows = [row for row in rows if row.get("category_id") == category]
    if payee:
        rows = [row for row in rows if payee.casefold() in (row.get("payee_name") or "").casefold()]
    total = len(rows)
    rows = rows[:limit] if limit else rows
    if fields:
        names = [name.strip() for name in fields.split(",")]
        rows = [{name: row.get(name) for name in names} for row in rows]
    complete = len(rows) == total and not (account or category or payee or fields)
    s.output.write(
        {
            "transactions": rows,
            "server_knowledge": result.get("server_knowledge") if complete else None,
        },
        budget=s.budget,
        since=start.isoformat(),
        until=end.isoformat(),
        total_count=total,
        returned_count=len(rows),
        truncated=len(rows) < total,
    )


@transactions_app.command("view")
def transaction_view(ctx: typer.Context, transaction_id: str):
    s = state(ctx)
    s.output.write({"transaction": s.client.transaction(s.budget, transaction_id)}, budget=s.budget)


@transactions_app.command("categorize")
def transaction_categorize(
    ctx: typer.Context,
    transaction_id: str,
    category: Annotated[str, typer.Option(help="Destination category UUID.")],
    dry_run: bool = False,
):
    s = state(ctx)
    result = operations.categorize(
        s.client, s.budget, [{"id": transaction_id, "category_id": category}], dry_run=dry_run
    )
    s.output.write(result, budget=s.budget)


@transactions_app.command("categorize-batch")
def transaction_categorize_batch(
    ctx: typer.Context,
    file: Annotated[str, typer.Option(help="JSON array of {id, category_id}; '-' reads stdin.")],
    dry_run: bool = False,
):
    updates = read_json(file)
    if not isinstance(updates, list):
        raise CliError("invalid_batch", "Input must be a JSON array.", 2)
    s = state(ctx)
    s.output.write(
        operations.categorize(s.client, s.budget, updates, dry_run=dry_run), budget=s.budget
    )


class GroupBy(StrEnum):
    category = "category"
    payee = "payee"
    month = "month"


@reports_app.command("spending")
def report_spending(
    ctx: typer.Context,
    month: str | None = None,
    since: str | None = None,
    until: str | None = None,
    group_by: GroupBy = GroupBy.category,
):
    if month and not since and not until:
        start, end = month_range(month)
    elif not month and since and until:
        start, end = parse_date(since), parse_date(until)
    else:
        raise CliError("date_range_required", "Use --month YYYY-MM or both --since and --until.", 2)
    if start > end:
        raise CliError("invalid_range", "Start date must be on or before end date.", 2)
    s = state(ctx)
    plan = s.client.plan(s.budget)
    transactions = s.client.transactions(s.budget, since=start, until=end)["transactions"]
    result = spending(
        transactions,
        s.client.categories(s.budget),
        accounts=s.client.accounts(s.budget),
        group_by=group_by.value,
    )
    s.output.write(
        result,
        budget=plan["id"],
        currency=(plan.get("currency_format") or {}).get("iso_code"),
        since=start.isoformat(),
        until=end.isoformat(),
    )


def main():
    try:
        result = app(standalone_mode=False)
        if isinstance(result, int):
            raise SystemExit(result)
    except click.ClickException as exc:
        emit_error(CliError("usage_error", exc.format_message(), 2))
        raise SystemExit(2) from exc
    except typer.Abort:
        emit_error(CliError("cancelled", "Command cancelled.", 130))
        raise SystemExit(130) from None
    except Exception as exc:
        # SDK and credential exceptions can contain tokens. Never print their repr or locals.
        emit_error(
            CliError(
                "unexpected_error",
                "Unexpected failure. Inspect the command and report a reproducible case.",
            )
        )
        raise SystemExit(1) from exc
