from conftest import BUDGET, DINING, GROCERIES

from ynab_cli.reports import spending


def test_spending_counts_split_children_once_refunds_and_excludes_transfers(fake):
    transactions = [
        {
            "id": "split",
            "date": "2026-10-01",
            "amount": -30000,
            "subtransactions": [
                {"amount": -10000, "category_id": GROCERIES},
                {"amount": -20000, "category_id": DINING},
            ],
        },
        {"id": "refund", "date": "2026-10-02", "amount": 2000, "category_id": GROCERIES},
        {"id": "transfer", "date": "2026-10-03", "amount": -500000, "transfer_account_id": "other"},
        {"id": "salary", "date": "2026-10-04", "amount": 800000, "category_id": None},
        {"id": "deleted", "deleted": True, "amount": -9999},
    ]
    result = spending(transactions, fake.categories(BUDGET), group_by="category")
    assert result["net_spending"] == 28000
    assert result["net_spending_decimal"] == "28.000"
    assert result["groups"][0]["name"] == "Dining"
    assert result["groups"][1]["refunds"] == 2000
    assert result["excluded"] == {"transfers": 1, "income": 1, "deleted": 1, "tracking_accounts": 0}


def test_categorized_transfer_to_tracking_account_counts_as_spending(fake):
    result = spending(
        [
            {
                "id": "investment",
                "date": "2026-10-01",
                "amount": -10000,
                "account_id": "account-1",
                "transfer_account_id": "tracking",
                "category_id": GROCERIES,
            },
            {
                "id": "opposite",
                "date": "2026-10-01",
                "amount": 10000,
                "account_id": "tracking",
                "transfer_account_id": "account-1",
            },
        ],
        fake.categories(BUDGET),
        group_by="category",
        accounts=[{"id": "account-1", "on_budget": True}, {"id": "tracking", "on_budget": False}],
    )
    assert result["net_spending"] == 10000
    assert result["excluded"]["tracking_accounts"] == 1


def test_reports_do_not_require_spending_to_be_positive(fake):
    result = spending(
        [{"id": "refund", "date": "2026-10-02", "amount": 2000, "category_id": GROCERIES}],
        fake.categories(BUDGET),
        group_by="month",
    )
    assert result["net_spending"] == -2000
