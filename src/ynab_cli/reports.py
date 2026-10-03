"""Reporting is deterministic and uses integer amounts throughout."""

from collections import defaultdict

from ynab_cli.money import from_milliunits


def spending(
    transactions: list[dict],
    groups: list[dict],
    *,
    group_by: str,
    accounts: list[dict] | None = None,
) -> dict:
    budget_accounts = {account["id"] for account in accounts or [] if account.get("on_budget")}
    category_names = {
        category["id"]: category["name"] for group in groups for category in group["categories"]
    }
    income_ids = {
        category["id"]
        for group in groups
        for category in group["categories"]
        if category.get("internal")
        and category.get("name") in {"Inflow: Ready to Assign", "Inflow: To be Budgeted"}
    }
    totals = defaultdict(lambda: {"outflows": 0, "refunds": 0, "count": 0})
    names: dict[str, str] = {}
    excluded = {"deleted": 0, "transfers": 0, "income": 0, "tracking_accounts": 0}
    for tx in transactions:
        if tx.get("deleted"):
            excluded["deleted"] += 1
            continue
        if accounts is not None and tx.get("account_id") not in budget_accounts:
            excluded["tracking_accounts"] += 1
            continue
        entries = tx.get("subtransactions") or [tx]
        for entry in entries:
            if entry.get("deleted"):
                continue
            transfer = entry.get("transfer_account_id") or tx.get("transfer_account_id")
            if transfer and (accounts is None or transfer in budget_accounts):
                excluded["transfers"] += 1
                continue
            category = entry.get("category_id")
            amount = entry["amount"]
            if category in income_ids or (category is None and amount > 0):
                excluded["income"] += 1
                continue
            if group_by == "category":
                key = category or "uncategorized"
                names[key] = category_names.get(key, "Uncategorized" if category is None else key)
            elif group_by == "payee":
                key = entry.get("payee_id") or tx.get("payee_id") or "unknown"
                names[key] = entry.get("payee_name") or tx.get("payee_name") or "Unknown payee"
            else:
                key = tx["date"][:7]
                names[key] = key
            totals[key]["outflows"] += max(0, -amount)
            totals[key]["refunds"] += max(0, amount)
            totals[key]["count"] += 1
    rows = [
        {
            "id": key,
            "name": names[key],
            **value,
            "net_spending": value["outflows"] - value["refunds"],
            "net_spending_decimal": from_milliunits(value["outflows"] - value["refunds"]),
        }
        for key, value in totals.items()
    ]
    rows.sort(
        key=lambda row: row["id"] if group_by == "month" else (-row["net_spending"], row["name"])
    )
    net = sum(row["net_spending"] for row in rows)
    return {
        "groups": rows,
        "net_spending": net,
        "net_spending_decimal": from_milliunits(net),
        "excluded": excluded,
        "group_by": group_by,
    }
