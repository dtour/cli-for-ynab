import json
import subprocess
import sys

import pytest
from conftest import BUDGET, GROCERIES, OTHER_BUDGET
from typer.testing import CliRunner

from ynab_cli import cli
from ynab_cli.config import write_config

runner = CliRunner()


@pytest.fixture
def wired(fake, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda *args: fake)
    write_config({"aliases": {"main": BUDGET, "other": OTHER_BUDGET}})
    return fake


def test_help_needs_no_credentials():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "transactions" in result.stdout


def test_agent_guide_needs_no_credentials():
    result = runner.invoke(cli.app, ["guide"])
    assert result.exit_code == 0, result.exception
    assert "categorization automatically" in result.stdout
    assert "Only after approval" in result.stdout


def test_alias_selects_other_budget(wired):
    result = runner.invoke(cli.app, ["--budget", "other", "transactions", "list"])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.stdout)["meta"]["budget_id"] == OTHER_BUDGET
    assert wired.reads[0][0] == OTHER_BUDGET


def test_missing_budget_is_json_error(wired):
    result = runner.invoke(cli.app, ["accounts", "list"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "budget_required"


def test_list_discloses_truncation(wired):
    wired.rows *= 3
    result = runner.invoke(cli.app, ["--budget", "main", "transactions", "list", "--limit", "1"])
    meta = json.loads(result.stdout)["meta"]
    assert meta["truncated"] is True
    assert meta["total_count"] == 3


def test_target_command_produces_preview_only(wired):
    result = runner.invoke(
        cli.app,
        [
            "--budget",
            "main",
            "categories",
            "target",
            GROCERIES,
            "--amount",
            "300.50",
            "--frequency",
            "monthly",
        ],
    )
    assert result.exit_code == 0, result.exception
    data = json.loads(result.stdout)
    assert data["data"]["proposal"]["operations"][0]["after"]["goal_target"] == 300500
    assert data["meta"]["status"] == "requires_approval"
    assert not wired.writes


def test_categorize_batch_accepts_stdin(wired):
    result = runner.invoke(
        cli.app,
        ["--budget", "main", "transactions", "categorize-batch", "--file", "-"],
        input=json.dumps([{"id": "tx-1", "category_id": GROCERIES}]),
    )
    assert result.exit_code == 0, result.exception
    assert len(wired.writes) == 1


def test_invalid_dates_fail_before_api(wired):
    result = runner.invoke(
        cli.app, ["--budget", "main", "reports", "spending", "--month", "2026-13"]
    )
    assert result.exit_code == 2
    assert not wired.reads


def test_delta_queries_require_complete_results(wired):
    result = runner.invoke(
        cli.app, ["--budget", "main", "transactions", "list", "--knowledge", "17"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_delta_query"
    assert not wired.reads


def test_delta_query_forwards_cursor_and_retains_tombstones(wired):
    wired.rows = [{"id": "deleted-id", "deleted": True}]
    result = runner.invoke(
        cli.app,
        [
            "--budget",
            "main",
            "transactions",
            "list",
            "--knowledge",
            "17",
            "--since",
            "2026-01-01",
            "--until",
            "2026-10-01",
            "--limit",
            "0",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert wired.reads[0][1]["knowledge"] == 17
    assert json.loads(result.stdout)["data"]["transactions"] == wired.rows


def test_saved_proposals_are_private_and_never_overwritten(wired, tmp_path):
    path = tmp_path / "proposal.json"
    args = [
        "--budget",
        "main",
        "categories",
        "allocate",
        GROCERIES,
        "--month",
        "2026-10",
        "--amount",
        "300",
        "--save",
        str(path),
    ]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.exception
    contents = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(contents)["budget_id"] == BUDGET
    repeated = runner.invoke(cli.app, args)
    assert repeated.exit_code == 2
    assert path.read_bytes() == contents
    assert not wired.writes


def test_apply_rejects_conflicting_explicit_budget(wired, tmp_path):
    path = tmp_path / "proposal.json"
    result = runner.invoke(
        cli.app,
        [
            "--budget",
            "main",
            "categories",
            "allocate",
            GROCERIES,
            "--month",
            "2026-10",
            "--amount",
            "300",
            "--save",
            str(path),
        ],
    )
    assert result.exit_code == 0
    identifier = json.loads(path.read_text())["id"]
    result = runner.invoke(
        cli.app,
        ["--budget", "other", "changes", "apply", str(path), "--approve", identifier],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "budget_mismatch"
    assert not wired.writes


@pytest.mark.parametrize(
    "args,code", [(["--help"], 0), (["--version"], 0), (["--nonsense"], 2), (["auth", "login"], 2)]
)
def test_process_exit_codes_and_no_tracebacks(args, code):
    result = subprocess.run(
        [sys.executable, "-m", "ynab_cli", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == code, result.stderr
    assert "Traceback" not in result.stderr
    if code:
        assert json.loads(result.stderr)["error"]
