"""The only module that knows YNAB's generated SDK and transport exceptions."""

import json
import re
from datetime import date

import ynab
from pydantic import BaseModel, ValidationError
from urllib3.exceptions import HTTPError
from ynab.exceptions import ApiException

from ynab_cli.auth import credential
from ynab_cli.errors import CliError


class ExactApiClient(ynab.ApiClient):
    def sanitize_for_serialization(self, obj):
        # Generated to_dict() methods drop some explicit nulls. Preserve exactly
        # the elected fields, including null and empty arrays, throughout a body.
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(mode="json", by_alias=True, exclude_unset=True)
        return super().sanitize_for_serialization(obj)


class Client:
    def __init__(self, token: str | None = None):
        self._token = token or credential()[0]
        self._api = ExactApiClient(ynab.Configuration(access_token=self._token, retries=0))
        self._plans: list[dict] | None = None

    def close(self) -> None:
        self._api.rest_client.pool_manager.clear()

    def _call(self, function, *args, **kwargs) -> dict:
        try:
            response = function(*args, **kwargs, _request_timeout=(10.0, 30.0))
            if response is None or response.data is None:
                raise CliError(
                    "invalid_response",
                    "YNAB returned no usable result. "
                    "Check the current state before retrying a write.",
                )
            return compact_amounts(response.data.model_dump(mode="json", by_alias=True))
        except ApiException as exc:
            detail = "YNAB rejected the request."
            try:
                detail = str(json.loads(exc.body or "{}")["error"]["detail"])
            except (ValueError, KeyError, TypeError):
                pass
            detail = detail.replace(self._token, "[REDACTED]")
            detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", detail)[:500]
            status = exc.status or 0
            codes = {
                401: ("not_authenticated", 3),
                403: ("access_denied", 3),
                404: ("not_found", 4),
                409: ("conflict", 4),
                429: ("rate_limited", 5),
            }
            code, exit_code = codes.get(status, ("api_error", 1))
            raise CliError(code, detail, exit_code, status) from exc
        except ValidationError as exc:
            raise CliError(
                "sdk_validation_error", "The request or API response failed validation.", 2
            ) from exc
        except (HTTPError, OSError) as exc:
            raise CliError(
                "network_error",
                "YNAB could not be reached reliably. "
                "A write may have succeeded; inspect before retrying.",
                5,
            ) from exc

    def user(self) -> dict:
        return self._call(ynab.UserApi(self._api).get_user)["user"]

    def invoke(
        self, operation: str, budget: str | None, params: dict, body: dict | None = None
    ) -> dict:
        from ynab_cli.api_registry import endpoint

        spec = endpoint(operation)
        parsed, data = spec.validate(params, body)
        kwargs = {key: getattr(parsed, key) for key in parsed.model_fields_set}
        if spec.budget_scoped:
            kwargs["plan_id"] = budget
        if spec.body_parameter:
            kwargs[spec.body_parameter] = data
        return self._call(getattr(spec.api_class(self._api), spec.name), **kwargs)

    def plans(self) -> list[dict]:
        if self._plans is None:
            self._plans = self._call(ynab.PlansApi(self._api).get_plans)["plans"]
        return self._plans

    def plan(self, plan_id: str) -> dict:
        matches = [plan for plan in self.plans() if plan["id"] == plan_id]
        if len(matches) != 1:
            raise CliError(
                "budget_not_found",
                "Select a budget ID or configured alias from 'ynab budgets list'.",
                4,
            )
        return matches[0]

    def accounts(self, plan_id: str) -> list[dict]:
        return self._call(ynab.AccountsApi(self._api).get_accounts, plan_id)["accounts"]

    def categories(self, plan_id: str) -> list[dict]:
        return self._call(ynab.CategoriesApi(self._api).get_categories, plan_id)["category_groups"]

    def category(self, plan_id: str, category_id: str, month: date | None = None) -> dict:
        api = ynab.CategoriesApi(self._api)
        if month:
            return self._call(api.get_month_category_by_id, plan_id, month, category_id)["category"]
        return self._call(api.get_category_by_id, plan_id, category_id)["category"]

    def transactions(
        self,
        plan_id: str,
        *,
        since: date,
        until: date | None = None,
        kind: str | None = None,
        knowledge: int | None = None,
    ) -> dict:
        return self._call(
            ynab.TransactionsApi(self._api).get_transactions,
            plan_id,
            since_date=since,
            until_date=until,
            type=kind,
            last_knowledge_of_server=knowledge,
        )

    def transaction(self, plan_id: str, transaction_id: str) -> dict:
        return self._call(
            ynab.TransactionsApi(self._api).get_transaction_by_id, plan_id, transaction_id
        )["transaction"]

    def categorize(self, plan_id: str, updates: list[dict]) -> dict:
        # SDK serialization keeps absent fields absent: only ID and category are sent.
        data = ynab.PatchTransactionsWrapper.model_validate({"transactions": updates})
        return self._call(ynab.TransactionsApi(self._api).update_transactions, plan_id, data)

    def allocate(self, plan_id: str, category_id: str, month: date, amount: int) -> dict:
        data = ynab.PatchMonthCategoryWrapper(category=ynab.SaveMonthCategory(budgeted=amount))
        return self._call(
            ynab.CategoriesApi(self._api).update_month_category,
            plan_id,
            month,
            category_id,
            data,
        )["category"]

    def target(self, plan_id: str, category_id: str, changes: dict) -> dict:
        data = ynab.PatchCategoryWrapper(category=ynab.ExistingCategory.model_validate(changes))
        return self._call(
            ynab.CategoriesApi(self._api).update_category, plan_id, category_id, data
        )["category"]


def compact_amounts(value):
    """Use one unambiguous money representation; omit API display duplicates."""
    if isinstance(value, list):
        return [compact_amounts(item) for item in value]
    if isinstance(value, dict):
        return {
            key: compact_amounts(item)
            for key, item in value.items()
            if not key.endswith(("_currency", "_formatted"))
        }
    return value
