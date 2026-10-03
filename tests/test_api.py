import copy
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
import ynab
from conftest import BUDGET, DINING, GROCERIES, OTHER_BUDGET, FakeClient
from typer.testing import CliRunner
from urllib3 import HTTPResponse
from ynab.exceptions import ApiException
from ynab.rest import RESTResponse

from ynab_cli import api_operations, cli
from ynab_cli.api_registry import ENDPOINTS, endpoint
from ynab_cli.client import Client
from ynab_cli.config import config_path, write_config
from ynab_cli.errors import CliError

ACCOUNT = "55555555-5555-4555-8555-555555555555"
PAYEE = "66666666-6666-4666-8666-666666666666"
MONTH = "2026-10-01"
TX = {"account_id": ACCOUNT, "date": "2026-10-01", "amount": -12500, "category_id": GROCERIES}
SCHEDULED = {**TX, "frequency": "monthly"}
BODIES = {
    "create_account": {"account": {"name": "Checking", "type": "checking", "balance": 0}},
    "create_category": {"category": {"name": "Coffee", "category_group_id": DINING}},
    "create_category_group": {"category_group": {"name": "Everyday"}},
    "update_category": {"category": {"name": "Food"}},
    "update_category_group": {"category_group": {"name": "Essentials"}},
    "update_month_category": {"category": {"budgeted": 12345}},
    "create_payee": {"payee": {"name": "Cafe"}},
    "update_payee": {"payee": {"name": "New cafe"}},
    "create_transaction": {"transaction": TX},
    "update_transaction": {"transaction": {"memo": "reviewed"}},
    "update_transactions": {"transactions": [{"id": "tx-1", "memo": "reviewed"}]},
    "create_scheduled_transaction": {"scheduled_transaction": SCHEDULED},
    "update_scheduled_transaction": {"scheduled_transaction": SCHEDULED},
}
PARAMETERS = {
    "account_id": ACCOUNT,
    "category_id": GROCERIES,
    "category_group_id": DINING,
    "payee_id": PAYEE,
    "payee_location_id": PAYEE,
    "transaction_id": "tx-1",
    "scheduled_transaction_id": "scheduled-1",
    "month": MONTH,
}
# Independent expectations for SDK transport routing, including special write verbs.
ROUTES = {
    "get_user": ("GET", "/user"),
    "get_plans": ("GET", "/plans"),
    "get_plan_by_id": ("GET", ""),
    "get_plan_settings_by_id": ("GET", "/settings"),
    "get_accounts": ("GET", "/accounts"),
    "get_account_by_id": ("GET", f"/accounts/{ACCOUNT}"),
    "create_account": ("POST", "/accounts"),
    "get_categories": ("GET", "/categories"),
    "get_category_by_id": ("GET", f"/categories/{GROCERIES}"),
    "get_month_category_by_id": ("GET", f"/months/{MONTH}/categories/{GROCERIES}"),
    "create_category": ("POST", "/categories"),
    "create_category_group": ("POST", "/category_groups"),
    "update_category": ("PATCH", f"/categories/{GROCERIES}"),
    "update_category_group": ("PATCH", f"/category_groups/{DINING}"),
    "update_month_category": ("PATCH", f"/months/{MONTH}/categories/{GROCERIES}"),
    "get_plan_months": ("GET", "/months"),
    "get_plan_month": ("GET", f"/months/{MONTH}"),
    "get_payees": ("GET", "/payees"),
    "get_payee_by_id": ("GET", f"/payees/{PAYEE}"),
    "create_payee": ("POST", "/payees"),
    "update_payee": ("PATCH", f"/payees/{PAYEE}"),
    "get_payee_locations": ("GET", "/payee_locations"),
    "get_payee_location_by_id": ("GET", f"/payee_locations/{PAYEE}"),
    "get_payee_locations_by_payee": ("GET", f"/payees/{PAYEE}/payee_locations"),
    "get_money_movements": ("GET", "/money_movements"),
    "get_money_movements_by_month": ("GET", f"/months/{MONTH}/money_movements"),
    "get_money_movement_groups": ("GET", "/money_movement_groups"),
    "get_money_movement_groups_by_month": ("GET", f"/months/{MONTH}/money_movement_groups"),
    "get_transactions": ("GET", "/transactions"),
    "get_transaction_by_id": ("GET", "/transactions/tx-1"),
    "get_transactions_by_account": ("GET", f"/accounts/{ACCOUNT}/transactions"),
    "get_transactions_by_category": ("GET", f"/categories/{GROCERIES}/transactions"),
    "get_transactions_by_payee": ("GET", f"/payees/{PAYEE}/transactions"),
    "get_transactions_by_month": ("GET", f"/months/{MONTH}/transactions"),
    "create_transaction": ("POST", "/transactions"),
    "update_transaction": ("PUT", "/transactions/tx-1"),
    "update_transactions": ("PATCH", "/transactions"),
    "delete_transaction": ("DELETE", "/transactions/tx-1"),
    "import_transactions": ("POST", "/transactions/import"),
    "get_scheduled_transactions": ("GET", "/scheduled_transactions"),
    "get_scheduled_transaction_by_id": ("GET", "/scheduled_transactions/scheduled-1"),
    "create_scheduled_transaction": ("POST", "/scheduled_transactions"),
    "update_scheduled_transaction": ("PUT", "/scheduled_transactions/scheduled-1"),
    "delete_scheduled_transaction": ("DELETE", "/scheduled_transactions/scheduled-1"),
}


def params_for(name):
    return {
        key: PARAMETERS[key]
        for key in endpoint(name).params_model.model_fields
        if key in PARAMETERS
    }


class ApiFake(FakeClient):
    def invoke(self, operation, budget, params, body=None):
        if endpoint(operation).writes:
            self.writes.append((operation, budget, copy.deepcopy(params), copy.deepcopy(body)))
            return {"ok": True}
        self.reads.append((budget, {"operation": operation, **params}))
        if operation == "get_transaction_by_id":
            return {"transaction": self.transaction(budget, params["transaction_id"])}
        if operation in {"get_category_by_id", "get_month_category_by_id"}:
            return {"category": self.category(budget, params["category_id"])}
        if operation == "get_categories":
            return {"category_groups": [{"id": DINING, "name": "Everyday", "categories": []}]}
        if operation == "get_payee_by_id":
            return {"payee": {"id": PAYEE, "name": "Cafe"}}
        if operation == "get_scheduled_transaction_by_id":
            return {"scheduled_transaction": {"id": "scheduled-1", **SCHEDULED}}
        if operation == "get_plan_by_id":
            return {"plan": self.plan(BUDGET)}
        return {"operation": operation, "params": params}


@pytest.fixture
def api_fake(monkeypatch):
    fake = ApiFake()
    monkeypatch.setattr(cli, "Client", lambda *args: fake)
    write_config({"aliases": {"main": BUDGET, "other": OTHER_BUDGET}})
    return fake


def test_registry_covers_every_sdk_operation_and_class():
    discovered = {}
    for class_name in dir(ynab):
        if not class_name.endswith("Api"):
            continue
        for name, _method in inspect.getmembers(getattr(ynab, class_name), inspect.isfunction):
            if name.startswith("_") or name.endswith(
                ("_with_http_info", "_without_preload_content")
            ):
                continue
            discovered[name] = class_name
    assert discovered == {name: spec.api_class.__name__ for name, spec in ENDPOINTS.items()}
    assert len(discovered) == 44
    assert sum(spec.writes for spec in ENDPOINTS.values()) == 16
    assert set(ROUTES) == set(ENDPOINTS)


@pytest.mark.parametrize("name", ENDPOINTS)
def test_every_operation_serializes_through_real_sdk(name, monkeypatch):
    client = Client("synthetic-token")
    transport = Mock(side_effect=ApiException(status=400, body='{"error":{"detail":"test"}}'))
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    try:
        with pytest.raises(CliError) as error:
            client.invoke(name, BUDGET, params_for(name), BODIES.get(name))
        assert error.value.code == "api_error"
        verb, suffix = ROUTES[name]
        prefix = "https://api.ynab.com/v1"
        if endpoint(name).budget_scoped:
            prefix += f"/plans/{BUDGET}"
        assert transport.call_args.args == (verb, prefix + suffix)
        assert transport.call_args.kwargs["body"] == BODIES.get(name)
        assert transport.call_count == 1
    finally:
        client.close()


@pytest.mark.parametrize("name", [name for name, spec in ENDPOINTS.items() if not spec.writes])
def test_every_read_is_accessible_from_cli(name, api_fake):
    result = CliRunner().invoke(
        cli.app,
        ["--budget", "other", "api", "call", name, "--params", json.dumps(params_for(name))],
    )
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["meta"]["operation"] == name
    assert not api_fake.writes


@pytest.mark.parametrize("name", [name for name, spec in ENDPOINTS.items() if spec.writes])
def test_every_write_previews_then_requires_matching_approval(name, api_fake, tmp_path):
    path = tmp_path / "proposal.json"
    args = [
        "--budget",
        "main",
        "api",
        "call",
        name,
        "--params",
        json.dumps(params_for(name)),
        "--save",
        str(path),
    ]
    if name in BODIES:
        args += ["--data", json.dumps(BODIES[name])]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.exception
    assert not api_fake.writes
    plan = api_operations.load_proposal(json.loads(path.read_text()))
    assert plan.budget_id == BUDGET
    assert path.stat().st_mode & 0o777 == 0o600
    wrong = CliRunner().invoke(cli.app, ["changes", "apply", str(path), "--approve", "yes"])
    assert wrong.exit_code == 2
    assert not api_fake.writes
    applied = CliRunner().invoke(cli.app, ["changes", "apply", str(path), "--approve", plan.id])
    assert applied.exit_code == 0, applied.exception
    assert api_fake.writes == [(name, BUDGET, params_for(name), BODIES.get(name))]
    assert json.loads(applied.stdout)["data"]["status"] == "applied"


@pytest.mark.parametrize("name", ENDPOINTS)
def test_every_operation_has_discoverable_json_schema_without_credentials(name):
    result = CliRunner().invoke(cli.app, ["api", "schema", name])
    assert result.exit_code == 0, result.exception
    schema = json.loads(result.stdout)["data"]
    assert schema["params_schema"]["additionalProperties"] is False
    assert "plan_id" not in schema["params_schema"]["properties"]
    assert schema["body_required"] == (name in BODIES)


def test_catalog_and_resource_filter_without_credentials():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["api", "list"])
    assert json.loads(result.stdout)["data"]["count"] == 44
    result = runner.invoke(cli.app, ["api", "list", "--resource", "scheduled"])
    assert json.loads(result.stdout)["data"]["count"] == 5


@pytest.mark.parametrize(
    "params,body",
    [
        ({"transaction_id": "tx-1", "_headers": {}}, {"transaction": {"memo": "a"}}),
        ({"transaction_id": "tx-1", "plan_id": OTHER_BUDGET}, {"transaction": {"memo": "a"}}),
        ({"transaction_id": "tx-1"}, {"transaction": {"memo_typo": "a"}}),
        ({"transaction_id": "tx-1"}, {"transaction": {"amount": 1.5}}),
        ({"transaction_id": "tx-1"}, {"transaction": {"amount": True}}),
        ({"transaction_id": "tx-1"}, {"transaction": {"subtransactions": [{"amount": "12"}]}}),
        (
            {"transaction_id": "tx-1"},
            {"transaction": {"subtransactions": [{"amount": 1, "extra": 2}]}},
        ),
        ({"transaction_id": "tx-1"}, {"transaction": {"var_date": "2026-10-01"}}),
    ],
)
def test_invalid_inputs_fail_before_any_request(params, body, api_fake):
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            "update_transaction",
            "--params",
            json.dumps(params),
            "--data",
            json.dumps(body),
        ],
    )
    assert result.exit_code == 2
    assert not api_fake.reads and not api_fake.writes


def test_exact_null_empty_array_and_absent_field_serialization(monkeypatch):
    client = Client("synthetic-token")
    transport = Mock(side_effect=ApiException(status=400))
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    body = {
        "transaction": {"category_id": None, "flag_color": None, "subtransactions": [], "memo": ""}
    }
    with pytest.raises(CliError):
        client.invoke("update_transaction", BUDGET, {"transaction_id": "tx-1"}, body)
    assert transport.call_args.kwargs["body"] == body
    client.close()


def test_general_transaction_changes_require_preview(api_fake):
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            "update_transaction",
            "--params",
            '{"transaction_id":"tx-1"}',
            "--data",
            json.dumps({"transaction": {"category_id": GROCERIES, "approved": True}}),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["meta"]["status"] == "requires_approval"
    assert not api_fake.writes


@pytest.mark.parametrize("name", ["update_transaction", "update_transactions"])
def test_api_category_only_updates_follow_automatic_policy(name, api_fake):
    body = (
        {"transaction": {"category_id": GROCERIES}}
        if name == "update_transaction"
        else {"transactions": [{"id": "tx-1", "category_id": GROCERIES}]}
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            name,
            "--params",
            json.dumps(params_for(name)),
            "--data",
            json.dumps(body),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert api_fake.writes[0] == (name, BUDGET, params_for(name), body)


def test_stale_update_proposal_writes_nothing(api_fake):
    plan = api_operations.preview(
        api_fake,
        "update_transaction",
        BUDGET,
        {"transaction_id": "tx-1"},
        BODIES["update_transaction"],
    )
    api_fake.rows[0]["memo"] = "changed elsewhere"
    with pytest.raises(CliError, match="record changed") as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.code == "stale_proposal"
    assert not api_fake.writes


def test_creation_cannot_be_replayed(api_fake):
    plan = api_operations.preview(
        api_fake, "create_transaction", BUDGET, {}, BODIES["create_transaction"]
    )
    api_operations.apply(api_fake, plan, plan.id)
    with pytest.raises(CliError) as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.code == "already_attempted"
    assert len(api_fake.writes) == 1
    receipt = config_path().parent / "changes" / f"{plan.id}.json"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text())["status"] == "succeeded"


def test_uncertain_write_cannot_be_replayed(api_fake, monkeypatch):
    plan = api_operations.preview(api_fake, "import_transactions", BUDGET, {}, None)
    invoke = Mock(side_effect=CliError("network_error", "timeout", 5))
    monkeypatch.setattr(api_fake, "invoke", invoke)
    with pytest.raises(CliError) as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.details["outcome_uncertain"] is True
    with pytest.raises(CliError) as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.code == "already_attempted"
    assert invoke.call_count == 1


def test_proposal_binds_body_and_canonical_budget(api_fake):
    plan = api_operations.preview(api_fake, "create_payee", "last-used", {}, BODIES["create_payee"])
    assert plan.budget_id == BUDGET
    value = plan.model_dump()
    value["body"]["payee"]["name"] = "changed"
    with pytest.raises(CliError) as error:
        api_operations.load_proposal(value)
    assert error.value.code == "invalid_proposal"


def test_read_query_dates_cursor_and_boolean_types():
    values, _ = api_operations.normalize(
        "get_transactions",
        {
            "since_date": "2026-01-01",
            "until_date": "2026-09-30",
            "last_knowledge_of_server": 42,
            "type": "uncategorized",
        },
        None,
    )
    assert values["last_knowledge_of_server"] == 42
    assert api_operations.normalize("get_plans", {"include_accounts": True}, None)[0] == {
        "include_accounts": True
    }
    with pytest.raises(CliError):
        api_operations.normalize("get_plans", {"include_accounts": "true"}, None)
    assert api_operations.normalize("get_plan_month", {"month": "2026-10"}, None)[0] == {
        "month": MONTH
    }


def test_input_file_and_stdin(api_fake, tmp_path):
    path = tmp_path / "params.json"
    path.write_text('{"payee_id":"' + PAYEE + '"}')
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["--budget", "main", "api", "call", "get_payee_by_id", "--params", "@" + str(path)],
    )
    assert result.exit_code == 0
    result = runner.invoke(
        cli.app,
        ["--budget", "main", "api", "call", "create_payee", "--data", "-"],
        input=json.dumps(BODIES["create_payee"]),
    )
    assert result.exit_code == 0, result.exception
    assert not api_fake.writes


def test_successful_creation_parses_real_sdk_201(monkeypatch):
    client = Client("synthetic-token")
    response = RESTResponse(
        HTTPResponse(
            body=json.dumps(
                {
                    "data": {
                        "payee": {"id": PAYEE, "name": "Cafe", "deleted": False},
                        "server_knowledge": 7,
                    }
                }
            ).encode(),
            status=201,
            headers={"content-type": "application/json"},
        )
    )
    monkeypatch.setattr(client._api.rest_client, "request", Mock(return_value=response))
    result = client.invoke("create_payee", BUDGET, {}, BODIES["create_payee"])
    assert result["payee"]["id"] == PAYEE
    assert result["server_knowledge"] == 7
    client.close()


def test_sdk_forwards_all_transaction_query_parameters(monkeypatch):
    client = Client("synthetic-token")
    transport = Mock(side_effect=ApiException(status=400))
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    params = {
        "since_date": "2026-01-01",
        "until_date": "2026-09-30",
        "type": "unapproved",
        "last_knowledge_of_server": 7,
    }
    with pytest.raises(CliError):
        client.invoke("get_transactions", BUDGET, params)
    url = transport.call_args.args[1]
    from urllib.parse import parse_qs, urlsplit

    assert parse_qs(urlsplit(url).query) == {key: [str(value)] for key, value in params.items()}
    client.close()


@pytest.mark.parametrize(
    "extra", [{"subtransactions": [{"id": "child"}]}, {"transfer_account_id": ACCOUNT}]
)
def test_automatic_api_categorization_refuses_complex_transactions(api_fake, extra):
    api_fake.rows[0].update(extra)
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            "update_transaction",
            "--params",
            '{"transaction_id":"tx-1"}',
            "--data",
            json.dumps({"transaction": {"category_id": GROCERIES}}),
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "complex_transaction"
    assert not api_fake.writes


def test_api_categorization_dry_run_makes_no_writes(api_fake):
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            "update_transaction",
            "--params",
            '{"transaction_id":"tx-1"}',
            "--data",
            json.dumps({"transaction": {"category_id": GROCERIES}}),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert not api_fake.writes


def test_clearing_category_requires_approval(api_fake):
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            "main",
            "api",
            "call",
            "update_transaction",
            "--params",
            '{"transaction_id":"tx-1"}',
            "--data",
            '{"transaction":{"category_id":null}}',
        ],
    )
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["meta"]["status"] == "requires_approval"
    assert not api_fake.writes


def test_batch_update_by_import_id_snapshots_matching_transactions(api_fake):
    api_fake.rows[0]["import_id"] = "import-1"
    body = {"transactions": [{"id": None, "import_id": "import-1", "memo": "reviewed"}]}
    plan = api_operations.preview(api_fake, "update_transactions", BUDGET, {}, body)
    assert plan.before["transactions"][0]["id"] == "tx-1"
    api_fake.rows[0]["amount"] = -10
    with pytest.raises(CliError) as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.code == "stale_proposal"
    assert not api_fake.writes


def test_api_apply_uses_saved_budget_even_after_default_changes(api_fake, tmp_path):
    plan = api_operations.preview(api_fake, "create_payee", BUDGET, {}, BODIES["create_payee"])
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps({"data": {"proposal": plan.model_dump()}}))
    write_config({"default_budget": OTHER_BUDGET})
    runner = CliRunner()
    conflict = runner.invoke(
        cli.app, ["--budget", OTHER_BUDGET, "changes", "apply", str(path), "--approve", plan.id]
    )
    assert conflict.exit_code == 2
    assert not api_fake.writes
    result = runner.invoke(cli.app, ["changes", "apply", str(path), "--approve", plan.id])
    assert result.exit_code == 0, result.exception
    assert api_fake.writes[0][1] == BUDGET


def test_receipt_failure_prevents_write(api_fake, monkeypatch):
    plan = api_operations.preview(api_fake, "create_payee", BUDGET, {}, BODIES["create_payee"])
    monkeypatch.setattr(api_operations.os, "fsync", Mock(side_effect=OSError("disk full")))
    with pytest.raises(CliError) as error:
        api_operations.apply(api_fake, plan, plan.id)
    assert error.value.code == "receipt_write_failed"
    assert not api_fake.writes


def test_concurrent_apply_sends_only_one_write(api_fake, monkeypatch):
    plan = api_operations.preview(api_fake, "create_payee", BUDGET, {}, BODIES["create_payee"])
    barrier = Barrier(2)
    original = api_fake.plan

    def synchronized_budget(budget):
        barrier.wait(timeout=5)
        return original(budget)

    monkeypatch.setattr(api_fake, "plan", synchronized_budget)

    def apply_once():
        try:
            return api_operations.apply(api_fake, plan, plan.id)["status"]
        except CliError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply_once(), range(2)))
    assert sorted(results) == ["already_attempted", "applied"]
    assert len(api_fake.writes) == 1


@pytest.mark.parametrize("name", ["get_user_with_http_info", "__init__", "call_api"])
def test_transport_helpers_are_not_exposed(name):
    result = CliRunner().invoke(cli.app, ["api", "call", name])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "unknown_operation"


def test_read_dry_run_needs_no_credentials():
    result = CliRunner().invoke(
        cli.app,
        [
            "--budget",
            BUDGET,
            "api",
            "call",
            "get_plan_month",
            "--params",
            '{"month":"current"}',
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["meta"]["status"] == "validated"
