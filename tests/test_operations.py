from datetime import date

import pytest
from conftest import BUDGET, DINING, GROCERIES

from ynab_cli import operations
from ynab_cli.errors import CliError

MONTH = date(2026, 10, 1)


def test_categorization_preserves_unrelated_fields_and_is_repeatable(fake):
    updates = [{"id": "tx-1", "category_id": GROCERIES}]
    operations.categorize(fake, BUDGET, updates, dry_run=False)
    assert fake.rows[0]["amount"] == -12500
    assert fake.rows[0]["memo"] == "keep this"
    assert fake.rows[0]["approved"] is False
    assert fake.writes[0][2] == updates
    result = operations.categorize(fake, BUDGET, updates, dry_run=False)
    assert len(fake.writes) == 1
    assert result["unchanged"] == 1


def test_categorization_preview_does_not_write(fake):
    result = operations.categorize(
        fake, BUDGET, [{"id": "tx-1", "category_id": GROCERIES}], dry_run=True
    )
    assert result["changes"][0]["category_name"] == "Groceries"
    assert fake.writes == []


@pytest.mark.parametrize("extra", [{"memo": "oops"}, {"amount": 0}, {"approved": True}])
def test_routine_categorization_cannot_change_other_fields(fake, extra):
    with pytest.raises(CliError):
        operations.categorize(
            fake, BUDGET, [{"id": "tx-1", "category_id": GROCERIES, **extra}], dry_run=False
        )
    assert fake.writes == []


@pytest.mark.parametrize(
    "extra", [{"subtransactions": [{"id": "split"}]}, {"transfer_account_id": "account-2"}]
)
def test_categorization_preserves_splits_and_transfers(fake, extra):
    fake.rows[0].update(extra)
    with pytest.raises(CliError) as error:
        operations.categorize(
            fake, BUDGET, [{"id": "tx-1", "category_id": GROCERIES}], dry_run=False
        )
    assert error.value.code == "complex_transaction"
    assert not fake.writes


def test_batch_validates_every_item_before_writing(fake):
    with pytest.raises(CliError):
        operations.categorize(
            fake,
            BUDGET,
            [{"id": "tx-1", "category_id": GROCERIES}, {"id": "missing", "category_id": DINING}],
            dry_run=False,
        )
    assert not fake.writes


def make_move(fake):
    return operations.proposal(
        fake, BUDGET, operations.move(fake, BUDGET, GROCERIES, DINING, MONTH, 25000)
    )


def test_move_preview_and_apply_preserve_total_assignments(fake):
    plan = make_move(fake)
    assert not fake.writes
    assert [op.after["budgeted"] for op in plan.operations] == [175000, 125000]
    result = operations.apply(fake, plan, plan.id)
    assert len(result["completed"]) == 2
    assert sum(c["budgeted"] for c in fake.cats.values()) == 300000
    repeat = operations.apply(fake, plan, plan.id)
    assert repeat["already_at_requested_value"] == [0, 1]
    assert len(fake.writes) == 2


def test_changed_destination_blocks_all_move_writes(fake):
    plan = make_move(fake)
    fake.cats[DINING]["balance"] += 100
    with pytest.raises(CliError) as error:
        operations.apply(fake, plan, plan.id)
    assert error.value.code == "stale_proposal"
    assert fake.writes == []


def test_move_partial_failure_is_reported_without_automatic_retry(fake, monkeypatch):
    plan = make_move(fake)
    allocate = fake.allocate

    def failing(budget, category, month, amount):
        if category == DINING:
            raise CliError("network_error", "Timeout", 5)
        return allocate(budget, category, month, amount)

    monkeypatch.setattr(fake, "allocate", failing)
    with pytest.raises(CliError) as error:
        operations.apply(fake, plan, plan.id)
    assert error.value.code == "apply_incomplete"
    assert error.value.details["failed_operation"] == 1
    assert error.value.details["outcome_uncertain"] is True
    assert len(error.value.details["completed"]) == 1
    assert len(fake.writes) == 1


def test_tampered_proposal_rejected(fake):
    data = make_move(fake).model_dump()
    data["operations"][0]["after"]["budgeted"] = 1
    with pytest.raises(CliError) as error:
        operations.load_proposal(data)
    assert error.value.code == "invalid_proposal"


@pytest.mark.parametrize("value", [{"data": None}, {"data": []}, {"data": "invalid"}, None])
def test_malformed_proposal_envelopes_are_input_errors(value):
    with pytest.raises(CliError) as error:
        operations.load_proposal(value)
    assert error.value.code == "invalid_proposal"
    assert error.value.exit_code == 2


def test_approval_must_match_exact_preview(fake):
    with pytest.raises(CliError) as error:
        operations.apply(fake, make_move(fake), "yes")
    assert error.value.code == "approval_mismatch"
    assert not fake.writes


def test_target_only_sends_elected_fields(fake):
    op = operations.target(
        fake, BUDGET, GROCERIES, {"goal_target": 300000, "goal_frequency": "monthly"}
    )
    plan = operations.proposal(fake, BUDGET, [op])
    operations.apply(fake, plan, plan.id)
    assert fake.writes[0][3] == {"goal_target": 300000, "goal_frequency": "monthly"}


def test_loan_target_rejects_unsupported_frequency(fake):
    fake.cats[GROCERIES]["goal_type"] = "DEBT"
    with pytest.raises(CliError) as error:
        operations.target(
            fake, BUDGET, GROCERIES, {"goal_target": 300000, "goal_frequency": "monthly"}
        )
    assert error.value.code == "unsupported_target"


def test_categorization_rejection_reason_is_visible(fake, monkeypatch):
    rejection = CliError("api_error", "Category is not valid for this transaction.", 1, 400)
    monkeypatch.setattr(fake, "categorize", lambda *args: (_ for _ in ()).throw(rejection))
    with pytest.raises(CliError) as error:
        operations.categorize(
            fake, BUDGET, [{"id": "tx-1", "category_id": GROCERIES}], dry_run=False
        )
    assert error.value.message == "Category is not valid for this transaction."
    assert error.value.status == 400


@pytest.mark.parametrize("kind", ["allocate", "target"])
def test_apply_incomplete_shows_why_ynab_rejected_the_write(fake, monkeypatch, kind):
    if kind == "allocate":
        plan = make_move(fake)
    else:
        op = operations.target(fake, BUDGET, GROCERIES, {"goal_target": 300000})
        plan = operations.proposal(fake, BUDGET, [op])
    rejection = CliError("api_error", "Goal target is not allowed", 1, 400)
    monkeypatch.setattr(fake, kind, lambda *args: (_ for _ in ()).throw(rejection))
    with pytest.raises(CliError) as error:
        operations.apply(fake, plan, plan.id)
    assert error.value.code == "apply_incomplete"
    assert error.value.status == 400
    assert error.value.details["cause"] == "api_error"
    assert error.value.details["reason"] == "Goal target is not allowed"
    assert error.value.details["outcome_uncertain"] is False
    assert error.value.message.startswith("Apply stopped: Goal target is not allowed. ")
