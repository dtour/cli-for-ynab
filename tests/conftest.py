import copy
import socket

import pytest

from ynab_cli import auth

BUDGET = "11111111-1111-4111-8111-111111111111"
OTHER_BUDGET = "22222222-2222-4222-8222-222222222222"
GROCERIES = "33333333-3333-4333-8333-333333333333"
DINING = "44444444-4444-4444-8444-444444444444"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("YNAB_API_TOKEN", raising=False)
    monkeypatch.delenv("YNAB_BUDGET_ID", raising=False)
    vault = {}
    monkeypatch.setattr(
        auth.keyring, "get_password", lambda service, account: vault.get((service, account))
    )
    monkeypatch.setattr(
        auth.keyring,
        "set_password",
        lambda service, account, token: vault.update({(service, account): token}),
    )
    monkeypatch.setattr(
        auth.keyring,
        "delete_password",
        lambda service, account: vault.pop((service, account), None),
    )

    def no_network(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)


class FakeClient:
    def __init__(self):
        self.rows = [
            {
                "id": "tx-1",
                "date": "2026-10-01",
                "amount": -12500,
                "payee_name": "Shop",
                "category_id": None,
                "approved": False,
                "account_id": "account-1",
                "memo": "keep this",
                "subtransactions": [],
            }
        ]
        self.cats = {
            GROCERIES: {
                "id": GROCERIES,
                "name": "Groceries",
                "budgeted": 200000,
                "balance": 150000,
                "activity": -50000,
            },
            DINING: {
                "id": DINING,
                "name": "Dining",
                "budgeted": 100000,
                "balance": 50000,
                "activity": -50000,
            },
        }
        self.writes = []
        self.reads = []
        self.closed = False

    def close(self):
        self.closed = True

    def user(self):
        return {"id": "user-1"}

    def plans(self):
        return [self.plan(BUDGET), self.plan(OTHER_BUDGET)]

    def plan(self, budget):
        assert budget in {BUDGET, OTHER_BUDGET}
        return {
            "id": budget,
            "name": "Main" if budget == BUDGET else "Other",
            "currency_format": {"iso_code": "GBP"},
        }

    def accounts(self, budget):
        return [{"id": "account-1", "name": "Current", "balance": 500000, "on_budget": True}]

    def categories(self, budget):
        return [{"name": "Everyday", "categories": copy.deepcopy(list(self.cats.values()))}]

    def category(self, budget, category, month=None):
        return copy.deepcopy(self.cats[category])

    def transaction(self, budget, transaction):
        return copy.deepcopy(next(tx for tx in self.rows if tx["id"] == transaction))

    def transactions(self, budget, **kwargs):
        self.reads.append((budget, kwargs))
        return {"transactions": copy.deepcopy(self.rows), "server_knowledge": 17}

    def categorize(self, budget, updates):
        self.writes.append(("categorize", budget, updates))
        for update in updates:
            next(tx for tx in self.rows if tx["id"] == update["id"]).update(update)
        return {"transaction_ids": [update["id"] for update in updates]}

    def allocate(self, budget, category, month, amount):
        self.writes.append(("allocate", budget, category, month, amount))
        self.cats[category]["balance"] += amount - self.cats[category]["budgeted"]
        self.cats[category]["budgeted"] = amount
        return self.category(budget, category)

    def target(self, budget, category, changes):
        self.writes.append(("target", budget, category, changes))
        self.cats[category].update(changes)
        return self.category(budget, category)


@pytest.fixture
def fake():
    return FakeClient()
