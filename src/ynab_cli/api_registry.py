"""An explicit SDK operation catalog with schemas derived from the pinned SDK."""

import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Annotated, get_args, get_origin

import ynab
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from ynab_cli.dates import parse_date, parse_month
from ynab_cli.errors import CliError
from ynab_cli.money import MAX_MILLIUNITS

# Explicit registration means an SDK upgrade cannot silently expose a new write.
CATALOG = {
    "UserApi": "get_user",
    "PlansApi": "get_plans get_plan_by_id get_plan_settings_by_id",
    "AccountsApi": "get_accounts get_account_by_id create_account",
    "CategoriesApi": (
        "get_categories get_category_by_id get_month_category_by_id "
        "create_category create_category_group update_category update_category_group "
        "update_month_category"
    ),
    "MonthsApi": "get_plan_months get_plan_month",
    "PayeesApi": "get_payees get_payee_by_id create_payee update_payee",
    "PayeeLocationsApi": (
        "get_payee_locations get_payee_location_by_id get_payee_locations_by_payee"
    ),
    "MoneyMovementsApi": (
        "get_money_movements get_money_movements_by_month "
        "get_money_movement_groups get_money_movement_groups_by_month"
    ),
    "TransactionsApi": (
        "get_transactions get_transaction_by_id get_transactions_by_account "
        "get_transactions_by_category get_transactions_by_month get_transactions_by_payee "
        "create_transaction update_transaction update_transactions delete_transaction "
        "import_transactions"
    ),
    "ScheduledTransactionsApi": (
        "get_scheduled_transactions get_scheduled_transaction_by_id "
        "create_scheduled_transaction update_scheduled_transaction delete_scheduled_transaction"
    ),
}


def unannotated(annotation):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


@dataclass
class Endpoint:
    name: str
    api_class: type

    @cached_property
    def parameters(self) -> dict:
        return {
            name: parameter
            for name, parameter in inspect.signature(
                getattr(self.api_class, self.name)
            ).parameters.items()
            if name != "self" and not name.startswith("_")
        }

    @property
    def writes(self) -> bool:
        return not self.name.startswith("get_")

    @property
    def budget_scoped(self) -> bool:
        return "plan_id" in self.parameters

    @cached_property
    def body_parameter(self) -> str | None:
        for name, param in self.parameters.items():
            annotation = unannotated(param.annotation)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                return name
        return None

    @property
    def body_model(self) -> type[BaseModel] | None:
        return (
            unannotated(self.parameters[self.body_parameter].annotation)
            if self.body_parameter
            else None
        )

    @cached_property
    def params_model(self) -> type[BaseModel]:
        fields = {
            name: (
                param.annotation,
                ... if param.default is inspect.Parameter.empty else param.default,
            )
            for name, param in self.parameters.items()
            if name not in {"plan_id", self.body_parameter}
        }
        return create_model(
            self.name + "Parameters", __config__=ConfigDict(extra="forbid"), **fields
        )

    def describe(self, *, schema: bool = False) -> dict:
        result = {
            "operation": self.name,
            "resource": self.api_class.__name__.removesuffix("Api"),
            "description": inspect.getdoc(getattr(self.api_class, self.name)).splitlines()[0],
            "writes": self.writes,
            "budget_required": self.budget_scoped,
            "body_required": self.body_parameter is not None,
            "approval": "preview_then_apply" if self.writes else "none",
        }
        if self.name in {"update_transaction", "update_transactions"}:
            result["approval"] = (
                "automatic_for_routine_categorization; otherwise_preview_then_apply"
            )
        if schema:
            result["params_schema"] = self.params_model.model_json_schema()
            result["body_schema"] = self.body_model.model_json_schema() if self.body_model else None
            if result["body_schema"]:
                close_schema_objects(result["body_schema"])
            result["money_unit"] = "milliunits"
            result["unknown_fields"] = "rejected_including_nested_objects"
            if "month" in self.params_model.model_fields:
                result["month_formats"] = ["YYYY-MM", "YYYY-MM-01", "current"]
        return result

    def validate(self, params: dict, body: dict | None) -> tuple[BaseModel, BaseModel | None]:
        try:
            params = dict(params) if isinstance(params, dict) else params
            if isinstance(params, dict) and isinstance(params.get("month"), str):
                month = params["month"]
                if month == "current":
                    month = datetime.now(UTC).date().replace(day=1).isoformat()
                elif len(month) == 7:
                    month = parse_month(month).isoformat()
                elif parse_date(month).day != 1:
                    raise ValueError("Month must be its first day")
                params["month"] = month
            parsed = self.params_model.model_validate_json(json.dumps(params), strict=True)
            if self.body_model:
                data = self.body_model.model_validate_json(
                    json.dumps(body, allow_nan=False),
                    strict=True,
                    extra="forbid",
                    by_alias=True,
                    by_name=False,
                )
                validate_aliases(body, data)
                validate_money(body)
            elif body is not None:
                raise ValueError("This operation has no request body")
            else:
                data = None
            values = parsed.model_dump(exclude_unset=True)
            if (
                values.get("last_knowledge_of_server", 0) is not None
                and values.get("last_knowledge_of_server", 0) < 0
            ):
                raise ValueError("Server knowledge cannot be negative")
            if values.get("type") not in {None, "uncategorized", "unapproved"}:
                raise ValueError("Unknown transaction type")
            if (
                values.get("since_date")
                and values.get("until_date")
                and values["since_date"] > values["until_date"]
            ):
                raise ValueError("Date range is reversed")
            return parsed, data
        except (ValidationError, ValueError, TypeError) as exc:
            details = None
            if isinstance(exc, ValidationError):
                details = {
                    "fields": [
                        {"path": list(error["loc"]), "type": error["type"]}
                        for error in exc.errors(include_input=False, include_context=False)
                    ]
                }
            raise CliError(
                "invalid_api_input",
                f"Invalid input. Run 'ynab api schema {self.name}' for accepted fields.",
                2,
                details=details,
            ) from exc


def close_schema_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema["additionalProperties"] = False
        for value in schema.values():
            close_schema_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            close_schema_objects(value)


def validate_aliases(value, parsed):
    # SDK models enable Python field names recursively; public JSON uses wire aliases.
    if isinstance(parsed, BaseModel) and isinstance(value, dict):
        fields = {field.alias or name: name for name, field in type(parsed).model_fields.items()}
        if not set(value) <= fields.keys():
            raise ValueError("Use public JSON field names from the schema")
        for alias, item in value.items():
            validate_aliases(item, getattr(parsed, fields[alias]))
    elif isinstance(parsed, list) and isinstance(value, list):
        for item, result in zip(value, parsed, strict=True):
            validate_aliases(item, result)


def validate_money(value):
    if isinstance(value, list):
        for item in value:
            validate_money(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"amount", "balance", "budgeted", "goal_target"} and item is not None:
                if type(item) is not int or abs(item) > MAX_MILLIUNITS:
                    raise ValueError("Money must use integer milliunits within the supported range")
            validate_money(item)


ENDPOINTS = {
    name: Endpoint(name, getattr(ynab, api_class))
    for api_class, names in CATALOG.items()
    for name in names.split()
}


def endpoint(name: str) -> Endpoint:
    try:
        return ENDPOINTS[name.replace("-", "_")]
    except KeyError as exc:
        raise CliError(
            "unknown_operation", "Unknown API operation. Run 'ynab api list'.", 2
        ) from exc
