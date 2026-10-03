"""Categorization is immediate; allocation and target changes produce reviewable proposals."""

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ynab_cli.client import Client
from ynab_cli.dates import parse_date
from ynab_cli.errors import CliError

TARGET_FIELDS = (
    "goal_type",
    "goal_target",
    "goal_target_date",
    "goal_needs_whole_amount",
    "goal_day",
    "goal_cadence",
    "goal_cadence_frequency",
)


def usable_category(category: dict) -> None:
    if category.get("deleted") or category.get("hidden") or category.get("internal"):
        raise CliError("invalid_category", "Choose an active, visible spending category.", 2)


def categorize(client: Client, budget: str, updates: list[dict], *, dry_run: bool) -> dict:
    if not updates or len(updates) > 100:
        raise CliError("invalid_batch", "Provide between 1 and 100 categorizations.", 2)
    seen: set[str] = set()
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"id", "category_id"}:
            raise CliError("invalid_batch", "Each item must contain only id and category_id.", 2)
        try:
            UUID(update["category_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise CliError("invalid_category", "category_id must be a category UUID.", 2) from exc
        if not isinstance(update["id"], str) or not update["id"] or update["id"] in seen:
            raise CliError("invalid_batch", "Transaction IDs must be nonempty and unique.", 2)
        seen.add(update["id"])
    categories = {c["id"]: c for g in client.categories(budget) for c in g["categories"]}
    if len(updates) == 1:
        tx = client.transaction(budget, updates[0]["id"])
        transactions = {tx["id"]: tx}
    else:
        # Fetch once for the entire batch, including IDs older than the API's one-year default.
        result = client.transactions(budget, since=date(1900, 1, 1))
        transactions = {tx["id"]: tx for tx in result["transactions"] if tx["id"] in seen}
    changes = []
    payload = []
    for update in updates:
        tx = transactions.get(update["id"])
        category = categories.get(update["category_id"])
        if not tx or tx.get("deleted"):
            raise CliError(
                "transaction_not_found",
                f"Transaction {update['id']} is not active in this budget.",
                4,
            )
        if not category:
            raise CliError(
                "category_not_found", "A category is not present in the selected budget.", 4
            )
        usable_category(category)
        if tx.get("transfer_account_id") or tx.get("subtransactions"):
            raise CliError(
                "complex_transaction",
                f"Transaction {tx['id']} is a transfer or split; "
                "routine categorization does not replace its structure.",
                2,
            )
        if tx.get("category_id") != category["id"]:
            changes.append(
                {
                    "id": tx["id"],
                    "payee": tx.get("payee_name"),
                    "amount": tx["amount"],
                    "before": tx.get("category_id"),
                    "after": category["id"],
                    "category_name": category["name"],
                }
            )
            payload.append(update)
    result = client.categorize(budget, payload) if payload and not dry_run else None
    return {
        "status": "preview" if dry_run else "applied",
        "changes": changes,
        "unchanged": len(updates) - len(changes),
        "result": result,
    }


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["allocation", "target"]
    category_id: str
    category_name: str
    month: str | None = None
    before: dict
    after: dict

    @model_validator(mode="after")
    def validate_fields(self):
        UUID(self.category_id)
        if self.kind == "allocation":
            if not self.month or parse_date(self.month).day != 1:
                raise ValueError("Allocation requires a calendar month")
            if set(self.before) != {"budgeted", "balance", "activity"}:
                raise ValueError("Incomplete allocation precondition")
            if set(self.after) != {"budgeted"} or type(self.after["budgeted"]) is not int:
                raise ValueError("Allocation requires integer milliunits")
        else:
            allowed = {
                "goal_target",
                "goal_frequency",
                "goal_target_date",
                "goal_needs_whole_amount",
            }
            if self.month is not None or set(self.before) != set(TARGET_FIELDS):
                raise ValueError("Invalid target precondition")
            if not set(self.after) <= allowed or "goal_target" not in self.after:
                raise ValueError("Invalid target fields")
            if type(self.after["goal_target"]) is not int or self.after["goal_target"] < 0:
                raise ValueError("Target must be nonnegative integer milliunits")
            if "goal_frequency" in self.after and self.after["goal_frequency"] not in {
                "monthly",
                "weekly",
                "yearly",
            }:
                raise ValueError("Invalid frequency")
            if "goal_target_date" in self.after:
                parse_date(self.after["goal_target_date"])
                if "goal_frequency" in self.after:
                    raise ValueError("Choose frequency or date")
            if (
                "goal_needs_whole_amount" in self.after
                and type(self.after["goal_needs_whole_amount"]) is not bool
            ):
                raise ValueError("Invalid rollover behavior")
        return self


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    id: str = ""
    created_at: str
    budget_id: str
    budget_name: str
    currency: str | None
    operations: list[Operation] = Field(min_length=1, max_length=100)

    def digest(self) -> str:
        serialized = json.dumps(
            self.model_dump(exclude={"id"}), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode()).hexdigest()


def proposal(client: Client, budget: str, operations: list[Operation]) -> Proposal:
    identity = client.plan(budget)
    result = Proposal(
        created_at=datetime.now(UTC).isoformat(),
        budget_id=identity["id"],
        budget_name=identity["name"],
        currency=(identity.get("currency_format") or {}).get("iso_code"),
        operations=operations,
    )
    result.id = result.digest()
    return result


def allocation(
    client: Client, budget: str, category_id: str, month: date, amount: int
) -> Operation:
    category = client.category(budget, category_id, month)
    usable_category(category)
    return Operation(
        kind="allocation",
        category_id=category["id"],
        category_name=category["name"],
        month=month.isoformat(),
        before={key: category[key] for key in ("budgeted", "balance", "activity")},
        after={"budgeted": amount},
    )


def move(
    client: Client, budget: str, source: str, destination: str, month: date, amount: int
) -> list[Operation]:
    if source == destination or amount <= 0:
        raise CliError("invalid_move", "Choose different categories and a positive amount.", 2)
    source_op = allocation(client, budget, source, month, 0)
    destination_op = allocation(client, budget, destination, month, 0)
    if source_op.before["balance"] < amount:
        raise CliError("insufficient_funds", "Source category has insufficient available funds.", 2)
    source_op.after["budgeted"] = source_op.before["budgeted"] - amount
    destination_op.after["budgeted"] = destination_op.before["budgeted"] + amount
    return [source_op, destination_op]


def target(client: Client, budget: str, category_id: str, changes: dict) -> Operation:
    category = client.category(budget, category_id)
    usable_category(category)
    if category.get("goal_type") == "DEBT" and set(changes) != {"goal_target"}:
        raise CliError("unsupported_target", "Loan payment targets accept only an amount.", 2)
    return Operation(
        kind="target",
        category_id=category["id"],
        category_name=category["name"],
        before={key: category.get(key) for key in TARGET_FIELDS},
        after=changes,
    )


def load_proposal(value: dict) -> Proposal:
    try:
        if isinstance(value, dict) and "data" in value:
            value = value["data"]
            if isinstance(value, dict):
                value = value.get("proposal", value)
        result = Proposal.model_validate(value)
        UUID(result.budget_id)
        if result.id != result.digest():
            raise ValueError("Digest does not match")
        resources = [(op.category_id, op.month) for op in result.operations]
        if len(resources) != len(set(resources)):
            raise ValueError("Duplicate operation")
        return result
    except (ValueError, TypeError, ValidationError) as exc:
        raise CliError(
            "invalid_proposal", "Proposal is invalid or has changed. Generate a new preview.", 2
        ) from exc


def apply(client: Client, plan: Proposal, approval: str) -> dict:
    if approval != plan.id:
        raise CliError("approval_mismatch", "--approve must match the reviewed proposal ID.", 2)
    client.plan(plan.budget_id)
    pending = []
    skipped = []
    for index, op in enumerate(plan.operations):
        current = client.category(
            plan.budget_id, op.category_id, parse_date(op.month) if op.month else None
        )
        usable_category(current)
        if op.kind == "allocation" and current["budgeted"] == op.after["budgeted"]:
            skipped.append(index)
            continue
        if any(current.get(key) != value for key, value in op.before.items()):
            raise CliError(
                "stale_proposal",
                "A category changed since preview. Generate a new proposal.",
                4,
                details={"operation": index, "category_id": op.category_id},
            )
        pending.append((index, op))
    completed = []
    for index, op in pending:
        try:
            if op.kind == "allocation":
                result = client.allocate(
                    plan.budget_id, op.category_id, parse_date(op.month), op.after["budgeted"]
                )
            else:
                result = client.target(plan.budget_id, op.category_id, op.after)
            completed.append({"operation": index, "category": result})
        except CliError as exc:
            raise CliError(
                "apply_incomplete",
                "Apply stopped. "
                "Review completed operations and current YNAB state before continuing.",
                exc.exit_code,
                exc.status,
                {
                    "proposal_id": plan.id,
                    "completed": completed,
                    "already_at_requested_value": skipped,
                    "failed_operation": index,
                    "cause": exc.code,
                    "outcome_uncertain": exc.code
                    in {"network_error", "invalid_response", "sdk_validation_error"}
                    or bool(exc.status and exc.status >= 500),
                },
            ) from exc
    return {
        "status": "applied",
        "proposal_id": plan.id,
        "completed": completed,
        "already_at_requested_value": skipped,
    }
