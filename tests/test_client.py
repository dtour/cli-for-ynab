import json
from datetime import date
from unittest.mock import Mock

import pytest
from conftest import BUDGET, GROCERIES
from urllib3 import HTTPResponse
from urllib3.exceptions import ReadTimeoutError
from ynab.exceptions import ApiException
from ynab.rest import RESTResponse

from ynab_cli.client import Client
from ynab_cli.errors import CliError


@pytest.fixture
def client():
    value = Client("test-secret-never-print")
    yield value
    value.close()


def respond(client, monkeypatch, body):
    response = RESTResponse(
        HTTPResponse(
            body=json.dumps(body).encode(), status=200, headers={"content-type": "application/json"}
        )
    )
    transport = Mock(return_value=response)
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    return transport


def test_sdk_sends_only_category_and_id_and_parses_real_200(client, monkeypatch):
    transport = respond(
        client, monkeypatch, {"data": {"transaction_ids": ["tx-1"], "server_knowledge": 5}}
    )
    result = client.categorize(BUDGET, [{"id": "tx-1", "category_id": GROCERIES}])
    assert result["transaction_ids"] == ["tx-1"]
    assert transport.call_args.args == (
        "PATCH",
        f"https://api.ynab.com/v1/plans/{BUDGET}/transactions",
    )
    assert transport.call_args.kwargs["body"] == {
        "transactions": [{"id": "tx-1", "category_id": GROCERIES}]
    }
    assert transport.call_args.kwargs["_request_timeout"] == (10.0, 30.0)
    assert client._api.configuration.retries == 0


def test_sdk_allocation_uses_exact_integer_and_explicit_month(client, monkeypatch):
    category = {
        "id": GROCERIES,
        "category_group_id": BUDGET,
        "name": "Groceries",
        "hidden": False,
        "internal": False,
        "deleted": False,
        "budgeted": 12345,
        "activity": 0,
        "balance": 12345,
        "balance_currency": 12.345,
    }
    transport = respond(
        client, monkeypatch, {"data": {"category": category, "server_knowledge": 5}}
    )
    result = client.allocate(BUDGET, GROCERIES, date(2026, 10, 1), 12345)
    assert transport.call_args.args[1].endswith(f"/months/2026-10-01/categories/{GROCERIES}")
    assert transport.call_args.kwargs["body"] == {"category": {"budgeted": 12345}}
    assert result["balance"] == 12345
    assert "balance_currency" not in result


def test_sdk_target_omits_unspecified_fields(client, monkeypatch):
    category = {
        "id": GROCERIES,
        "category_group_id": BUDGET,
        "name": "Groceries",
        "hidden": False,
        "internal": False,
        "deleted": False,
        "budgeted": 0,
        "activity": 0,
        "balance": 0,
    }
    transport = respond(
        client, monkeypatch, {"data": {"category": category, "server_knowledge": 5}}
    )
    client.target(BUDGET, GROCERIES, {"goal_target": 250000, "goal_frequency": "monthly"})
    assert transport.call_args.kwargs["body"] == {
        "category": {"goal_target": 250000, "goal_frequency": "monthly"}
    }


@pytest.mark.parametrize(("status", "code"), [(401, "not_authenticated"), (429, "rate_limited")])
def test_api_errors_are_structured_and_credentials_redacted(client, monkeypatch, status, code):
    transport = Mock(
        side_effect=ApiException(
            status=status,
            body=json.dumps(
                {"error": {"detail": "bad test-secret-never-print Bearer also-secret"}}
            ),
        )
    )
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    with pytest.raises(CliError) as error:
        client.user()
    assert error.value.code == code
    assert "secret" not in str(error.value)
    assert transport.call_count == 1


def test_timeout_does_not_retry_write(client, monkeypatch):
    transport = Mock(side_effect=ReadTimeoutError(None, "/transactions", "timed out"))
    monkeypatch.setattr(client._api.rest_client, "request", transport)
    with pytest.raises(CliError) as error:
        client.categorize(BUDGET, [{"id": "tx-1", "category_id": GROCERIES}])
    assert error.value.code == "network_error"
    assert transport.call_count == 1
