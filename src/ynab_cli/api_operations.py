"""Approval and replay handling for the complete API command layer."""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from ynab_cli import operations
from ynab_cli.api_registry import endpoint
from ynab_cli.client import Client
from ynab_cli.config import config_path
from ynab_cli.errors import CliError


class ApiProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[2] = 2
    kind: Literal["api"] = "api"
    id: str = ""
    created_at: str
    budget_id: str
    budget_name: str
    currency: str | None
    operation: str
    params: dict
    body: dict | None
    before: dict | None

    def digest(self) -> str:
        content = json.dumps(
            self.model_dump(exclude={"id"}), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(content.encode()).hexdigest()


def unwrap(value):
    if isinstance(value, dict) and "data" in value:
        value = value["data"]
    if isinstance(value, dict):
        value = value.get("proposal", value)
    return value


def normalize(operation: str, params: dict, body: dict | None) -> tuple[dict, dict | None]:
    parsed, data = endpoint(operation).validate(params, body)
    return (
        parsed.model_dump(mode="json", by_alias=True, exclude_unset=True),
        data.model_dump(mode="json", by_alias=True, exclude_unset=True) if data else None,
    )


def routine_updates(operation: str, params: dict, body: dict | None) -> list[dict] | None:
    """Only narrowly defined, category-only transaction updates can run automatically."""
    if not body:
        return None
    if operation == "update_transaction" and set(body) == {"transaction"}:
        tx = body["transaction"]
        if isinstance(tx, dict) and set(tx) == {"category_id"} and tx["category_id"]:
            return [{"id": params["transaction_id"], "category_id": tx["category_id"]}]
    if operation == "update_transactions" and set(body) == {"transactions"}:
        rows = body["transactions"]
        if (
            rows
            and len(rows) <= 100
            and all(
                set(row) == {"id", "category_id"} and row["id"] and row["category_id"]
                for row in rows
            )
        ):
            return rows
    return None


def categorize(
    client: Client, operation: str, budget: str, params: dict, body: dict, *, dry_run: bool
) -> dict:
    updates = routine_updates(operation, params, body)
    review = operations.categorize(client, budget, updates, dry_run=True)
    result = None
    if review["changes"] and not dry_run:
        if operation == "update_transactions":
            changed = {change["id"] for change in review["changes"]}
            body = {"transactions": [row for row in body["transactions"] if row["id"] in changed]}
        result = client.invoke(operation, budget, params, body)
    return {**review, "status": "preview" if dry_run else "applied", "result": result}


SINGLE_READS = {
    "update_transaction": ("get_transaction_by_id", "transaction"),
    "delete_transaction": ("get_transaction_by_id", "transaction"),
    "update_scheduled_transaction": ("get_scheduled_transaction_by_id", "scheduled_transaction"),
    "delete_scheduled_transaction": ("get_scheduled_transaction_by_id", "scheduled_transaction"),
    "update_payee": ("get_payee_by_id", "payee"),
    "update_category": ("get_category_by_id", "category"),
    "update_month_category": ("get_month_category_by_id", "category"),
}


def snapshot(
    client: Client, operation: str, budget: str, params: dict, body: dict | None
) -> dict | None:
    if operation in SINGLE_READS:
        read, key = SINGLE_READS[operation]
        value = client.invoke(read, budget, params)[key]
        if value.get("deleted"):
            raise CliError("not_found", "The selected record was deleted.", 4)
        return {key: value}
    if operation == "update_category_group":
        groups = client.invoke("get_categories", budget, {})["category_groups"]
        matches = [
            g for g in groups if g["id"] == params["category_group_id"] and not g.get("deleted")
        ]
        if not matches:
            raise CliError("not_found", "The category group was not found in this budget.", 4)
        return {"category_group": {k: v for k, v in matches[0].items() if k != "categories"}}
    if operation == "update_transactions":
        rows = client.transactions(budget, since=date(1900, 1, 1))["transactions"]
        selected = {}
        updates = body["transactions"]
        if not updates:
            raise CliError("invalid_api_input", "Provide at least one transaction update.", 2)
        for update in updates:
            key = "id" if update.get("id") else "import_id"
            identifier = update.get(key)
            if not identifier or (update.get("id") and update.get("import_id")):
                raise CliError("invalid_api_input", "Each update needs either id or import_id.", 2)
            matches = [row for row in rows if row.get(key) == identifier and not row.get("deleted")]
            if not matches:
                raise CliError(
                    "not_found", "An updated transaction was not found in this budget.", 4
                )
            for row in matches:
                if row["id"] in selected:
                    raise CliError(
                        "invalid_api_input", "A transaction appears in multiple updates.", 2
                    )
                selected[row["id"]] = row
        return {"transactions": [selected[key] for key in sorted(selected)]}
    return None


def preview(
    client: Client, operation: str, budget: str, params: dict, body: dict | None
) -> ApiProposal:
    spec = endpoint(operation)
    if not spec.writes:
        raise CliError("invalid_proposal", "Read operations cannot be saved as changes.", 2)
    params, body = normalize(spec.name, params, body)
    if spec.name == "create_transaction":
        selected = [key for key in ("transaction", "transactions") if body.get(key) is not None]
        if len(selected) != 1 or not body[selected[0]]:
            raise CliError(
                "invalid_api_input", "Supply transaction or a nonempty transactions array.", 2
            )
    if budget in {"last-used", "default"}:
        identity = client.invoke("get_plan_by_id", budget, {})["plan"]
    else:
        identity = client.plan(budget)
    plan = ApiProposal(
        created_at=datetime.now(UTC).isoformat(),
        budget_id=identity["id"],
        budget_name=identity["name"],
        currency=(identity.get("currency_format") or {}).get("iso_code"),
        operation=spec.name,
        params=params,
        body=body,
        before=snapshot(client, spec.name, identity["id"], params, body),
    )
    plan.id = plan.digest()
    return plan


def load_proposal(value) -> ApiProposal:
    try:
        result = ApiProposal.model_validate(unwrap(value))
        UUID(result.budget_id)
        spec = endpoint(result.operation)
        if not spec.writes or result.operation != spec.name or result.id != result.digest():
            raise ValueError("Invalid operation or digest")
        params, body = normalize(spec.name, result.params, result.body)
        if params != result.params or body != result.body:
            raise ValueError("Proposal must contain normalized values")
        return result
    except (ValueError, TypeError, ValidationError, CliError) as exc:
        raise CliError(
            "invalid_proposal", "API proposal is invalid or changed. Generate a new preview.", 2
        ) from exc


def apply(client: Client, plan: ApiProposal, approval: str) -> dict:
    # Revalidate even when called outside the CLI.
    plan = load_proposal(plan.model_dump())
    if approval != plan.id:
        raise CliError("approval_mismatch", "--approve must match the reviewed proposal ID.", 2)
    directory = config_path().parent / "changes"
    receipt = directory / f"{plan.id}.json"
    if receipt.exists():
        raise CliError(
            "already_attempted",
            "This API proposal was already attempted. Inspect YNAB before creating a new preview.",
            4,
        )
    client.plan(plan.budget_id)
    if snapshot(client, plan.operation, plan.budget_id, plan.params, plan.body) != plan.before:
        raise CliError(
            "stale_proposal", "The record changed since preview. Generate a new proposal.", 4
        )
    record = {
        "proposal_id": plan.id,
        "operation": plan.operation,
        "status": "started",
        "attempted_at": datetime.now(UTC).isoformat(),
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CliError(
            "already_attempted", "Another process already attempted this proposal.", 4
        ) from exc
    except OSError as exc:
        raise CliError(
            "receipt_write_failed", "Cannot record the write attempt; no API write was sent."
        ) from exc
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CliError(
            "receipt_write_failed", "Cannot persist the attempt; no API write was sent."
        ) from exc
    try:
        result = client.invoke(plan.operation, plan.budget_id, plan.params, plan.body)
    except CliError as exc:
        uncertain = exc.code in {
            "network_error",
            "invalid_response",
            "sdk_validation_error",
        } or bool(exc.status and exc.status >= 500)
        record.update(status="failed", cause=exc.code, outcome_uncertain=uncertain)
        finish_receipt(receipt, record)
        raise CliError(
            "api_apply_failed",
            "API write failed. Inspect YNAB before preparing another attempt.",
            exc.exit_code,
            exc.status,
            {"proposal_id": plan.id, "cause": exc.code, "outcome_uncertain": uncertain},
        ) from exc
    record["status"] = "succeeded"
    saved = finish_receipt(receipt, record)
    return {
        "status": "applied",
        "proposal_id": plan.id,
        "result": result,
        "receipt_finalized": saved,
    }


def finish_receipt(path, record) -> bool:
    try:
        # The file already exists, so even a crash during this update blocks replay.
        with path.open("w") as handle:
            json.dump(record, handle)
        return True
    except OSError:
        return False
